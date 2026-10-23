"""Parâmetros ajustáveis pelo painel, com padrão de fábrica.

Sem valor salvo no banco, vale o padrão (que, para a agenda e a rampa, vem do .env).
"""

import logging
import re
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Configuracao, NumeroStatus, WhatsappNumber
from app.services.rampa import Rampa, parse_rampa
from app.services.telefone import normalizar_telefone
from app.tenancy import exigir_cliente

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Campo:
    chave: str
    grupo: str
    rotulo: str
    tipo: str  # dias | hora | inteiro | decimal | rampa | texto (sem espaços) | frase | longo | simnao | telefone (opcional) | lista_dias
    ajuda: str = ""
    minimo: float | None = None
    maximo: float | None = None


CAMPOS = [
    Campo("empresa_nome", "Empresa", "Empresa que a IA representa", "frase"),
    Campo("atendente_nome", "Empresa", "Nome que a IA usa", "frase", "Entra no lugar de [Seu Nome] nos roteiros."),
    Campo("economia_texto", "Empresa", "Economia que pode ser citada", "frase",
          "Entra no lugar de \"até X%\" nos roteiros. Use só o que a empresa consegue sustentar."),
    Campo("campanha_oferta", "Campanha nova", "Oferta / promessa", "longo",
          "Já vem preenchida em cada campanha nova (dá para mudar nela). {marca}, {economia} e {atendente} viram os valores acima."),
    Campo("campanha_instrucoes", "Campanha nova", "Instruções para a IA", "longo",
          "Já vêm preenchidas em cada campanha nova. A Bruna segue isto em todas as conversas da campanha."),
    Campo("disparo_dias", "Disparo", "Dias de disparo", "dias"),
    Campo("disparo_inicio", "Disparo", "Início do horário comercial", "hora"),
    Campo("disparo_fim", "Disparo", "Fim do horário comercial", "hora"),
    Campo("intervalo_min_minutos", "Disparo", "Intervalo mínimo entre envios (minutos)", "decimal",
          "Por número. O sistema sorteia um intervalo entre o mínimo e o máximo.", 0.5, 120),
    Campo("intervalo_max_minutos", "Disparo", "Intervalo máximo entre envios (minutos)", "decimal", "", 0.5, 240),
    Campo("rampa", "Disparo", "Rampa de aquecimento", "rampa",
          "dia_inicial:limite, separados por vírgula. Ex.: 1:10,4:20,8:40 = 10/dia nos dias 1–3, 20 nos dias 4–7…"),
    Campo("pausa_taxa_minima", "Proteção", "Pausar número com taxa de resposta abaixo de (%)", "decimal",
          "0 desliga a pausa automática.", 0, 100),
    Campo("pausa_janela_envios", "Proteção", "…medida nos últimos N envios", "inteiro",
          "Só conta envios com pelo menos 6 horas, para dar tempo de o lead responder.", 10, 1000),
    Campo("pausa_falhas_seguidas", "Proteção", "Pausar número depois de N falhas seguidas no envio", "inteiro",
          "Falha seguida costuma ser chip bloqueado ou sessão caída. 0 desliga.", 0, 20),
    Campo("meta_diaria", "Super prospecção", "Meta de primeiras mensagens por dia (todos os números)", "inteiro",
          "Só nas campanhas de super prospecção. O sistema reparte a meta entre os números delas e espalha pelo "
          "horário de disparo. 0 = sem meta: cada número envia até o próprio limite.", 0, 20000),
    Campo("teto_por_numero", "Super prospecção", "Teto de envios por número por dia", "inteiro",
          "Só nas campanhas de super prospecção. Nenhum número passa disso, mesmo que a rampa permita. "
          "Conta primeira mensagem e follow-up.", 1, 500),
    Campo("limite_hora_numero", "Super prospecção", "Máximo de envios por número por hora", "inteiro",
          "Só nas campanhas de super prospecção. Evita rajadas no mesmo chip.", 1, 60),
    Campo("abastecer_ativo", "Abastecimento automático", "Buscar no Google Maps sozinho quando a fila baixar", "simnao",
          "Usa o plano de buscas da tela Super prospecção, uma busca por vez."),
    Campo("abastecer_estoque_dias", "Abastecimento automático", "Manter leads na fila para quantos dias", "decimal",
          "Multiplica a meta do dia (ou, sem meta, o que os números aguentam).", 0.5, 30),
    Campo("abastecer_gasto_diario", "Abastecimento automático", "Teto de gasto no Apify por dia (US$)", "decimal",
          "Conta o teto de cada busca criada no dia, automática ou não; o gasto real fica igual ou abaixo.", 0.5, 1000),
    Campo("reuniao_minutos", "Agenda", "Duração da reunião (minutos)", "inteiro", "", 10, 240),
    Campo("reuniao_buffer_minutos", "Agenda", "Intervalo entre reuniões (minutos)", "inteiro", "", 0, 120),
    Campo("agenda_antecedencia_horas", "Agenda", "Antecedência mínima (horas)", "inteiro", "", 0, 72),
    Campo("agenda_dias_a_frente", "Agenda", "Oferecer horários até quantos dias à frente", "inteiro", "", 1, 60),
    Campo("followup_ativo", "Follow-up", "Mandar follow-up para quem não respondeu", "simnao",
          "Sai pelo mesmo número da primeira mensagem, no horário de disparo e dentro do limite diário do número."),
    Campo("followup_dias", "Follow-up", "Dias do follow-up depois da primeira mensagem", "lista_dias",
          "Separados por vírgula, em ordem. Ex.: 2,5,10 = três follow-ups. Sem resposta 3 dias depois do último, "
          "o lead vira \"sem resposta\"."),
    Campo("notificar_telefone", "Notificações", "WhatsApp que recebe as notificações de reunião", "telefone",
          "Recebe cada reunião agendada ou cancelada e o resumo do dia. Não use um dos números de prospecção. "
          "Vazio desliga."),
    Campo("avisar_vendedor", "Notificações", "Avisar também o vendedor no WhatsApp dele", "simnao",
          "Desligado: só o WhatsApp de notificações recebe, e você repassa."),
    Campo("resumo_automatico", "Notificações", "Enviar o resumo das reuniões do dia automaticamente", "simnao",
          "Vai para o WhatsApp de notificações, só nos dias com reunião."),
    Campo("resumo_hora", "Notificações", "Horário do resumo automático", "hora"),
    Campo("alertar_chips", "Notificações", "Avisar quando um número cair ou for pausado", "simnao",
          "Vai para o WhatsApp de notificações: desconexão, pausa por taxa de resposta ou por falhas seguidas."),
    Campo("ia_modelo", "Inteligência artificial", "Modelo", "texto",
          "Nome do modelo ou combo no OmniRouter (ex.: cc/claude-sonnet-5, auto/claude-sonnet)."),
    Campo("pitch_em_sequencia", "Inteligência artificial", "Mandar Interesse, Desejo e Ação em sequência", "simnao",
          "Depois que o lead responde a Atenção, as três etapas saem uma atrás da outra, com poucos segundos entre elas. "
          "Desligado: uma etapa por resposta do lead."),
    Campo("ia_espera_segundos", "Inteligência artificial", "Esperar antes de responder (segundos)", "inteiro",
          "Junta as mensagens que o lead manda em sequência numa resposta só.", 3, 120),
]
CAMPOS_POR_CHAVE = {c.chave: c for c in CAMPOS}


def parse_dias_followup(texto: str) -> list[int]:
    """ "2, 5, 10" -> [2, 5, 10]. Levanta ValueError com a explicação."""
    try:
        dias = [int(parte) for parte in str(texto).replace(" ", "").split(",") if parte]
    except ValueError:
        raise ValueError("use números separados por vírgula, ex.: 2,5,10") from None
    if not dias:
        raise ValueError("informe pelo menos um dia, ex.: 2,5,10")
    if len(dias) > 5:
        raise ValueError("no máximo 5 follow-ups")
    if any(d < 1 or d > 60 for d in dias):
        raise ValueError("cada dia precisa estar entre 1 e 60")
    if any(b <= a for a, b in zip(dias, dias[1:])):
        raise ValueError("os dias precisam estar em ordem crescente, ex.: 2,5,10")
    return dias


def formatar_dias_followup(dias: list[int]) -> str:
    return ",".join(str(d) for d in dias)


def padroes() -> dict:
    s = get_settings()
    return {
        "disparo_dias": [0, 1, 2, 3, 4],
        "disparo_inicio": "09:00",
        "disparo_fim": "18:00",
        "intervalo_min_minutos": 2.0,
        "intervalo_max_minutos": 6.0,
        "rampa": s.rampa_limites,
        "pausa_taxa_minima": 5.0,
        "pausa_janela_envios": 50,
        "pausa_falhas_seguidas": 3,
        "meta_diaria": 0,
        "teto_por_numero": 50,
        "limite_hora_numero": 8,
        "alertar_chips": True,
        "abastecer_ativo": False,
        "abastecer_estoque_dias": 2.0,
        "abastecer_gasto_diario": 10.0,
        "reuniao_minutos": s.reuniao_minutos,
        "reuniao_buffer_minutos": s.reuniao_buffer_minutos,
        "agenda_antecedencia_horas": s.agenda_antecedencia_horas,
        "agenda_dias_a_frente": s.agenda_dias_a_frente,
        "ia_modelo": s.ia_modelo,
        "ia_espera_segundos": 20,
        "pitch_em_sequencia": True,
        "empresa_nome": "Brafort10",
        "atendente_nome": "Bruna",
        "economia_texto": "até 50%",
        "campanha_oferta": 'Produtos de limpeza profissional super concentrados da {marca}: rendem mais e reduzem o gasto com produtos em {economia}, sem perder qualidade nem segurança. Junto vêm o atendimento do "Dr. Limpeza" (consultoria e suporte pelo WhatsApp) e o treinamento da equipe de limpeza certificado pela ANVISA. Convite: uma análise gratuita do potencial de economia da empresa.',
        "campanha_instrucoes": 'Você é {atendente}, da {marca}, empresa de produtos de limpeza profissional.\n- Empresas pequenas: foque no produto concentrado e na economia ({economia} no gasto com produtos).\n- Empresas médias: foque no atendimento. O "Dr. Limpeza" dá consultoria e suporte direto pelo WhatsApp.\n- Empresas grandes: foque no treinamento da equipe, certificado pela ANVISA, e na padronização da limpeza.\n- O objetivo é agendar a análise gratuita: uma conversa rápida com o time, sem compromisso.\n- Não passe preço nem tabela por mensagem: explique que a análise mostra quanto dá para economizar e ofereça os horários.\n- "Já tenho fornecedor": a análise compara com o que a empresa usa hoje e não obriga a trocar.\n- "Não tenho tempo": a conversa é curta, e você ajusta ao melhor horário dela.\n- "Manda por e-mail": a análise depende de entender a rotina de limpeza, por isso a conversa.\n- Não prometa economia maior que {economia} nem resultados que a {marca} não garante.',
        "followup_ativo": True,
        "followup_dias": "2,5,10",
        "notificar_telefone": "",
        "avisar_vendedor": True,
        "resumo_automatico": False,
        "resumo_hora": "08:00",
    }


def valores(db: Session) -> dict:
    atuais = padroes()
    exigir_cliente(db)  # sem cliente, misturaria as configurações de todos
    for linha in db.query(Configuracao).all():
        if linha.chave in atuais:
            atuais[linha.chave] = linha.valor
    return atuais


def _converter(campo: Campo, bruto) -> object:
    if campo.tipo == "dias":
        dias = sorted({int(d) for d in bruto}) if isinstance(bruto, list) else []
        if not dias or any(d not in range(7) for d in dias):
            raise ConfigError("Escolha pelo menos um dia de disparo.")
        return dias
    texto = str(bruto).strip()
    if campo.tipo == "hora":
        if not re.fullmatch(r"\d{1,2}:\d{2}", texto):
            raise ConfigError(f"{campo.rotulo}: use o formato 09:00.")
        hora, minuto = map(int, texto.split(":"))
        if hora > 23 or minuto > 59:
            raise ConfigError(f"{campo.rotulo}: horário inválido.")
        return f"{hora:02d}:{minuto:02d}"
    if campo.tipo == "simnao":
        return bruto in (True, "1", "on", "sim", "true")
    if campo.tipo == "lista_dias":
        try:
            return formatar_dias_followup(parse_dias_followup(texto))
        except ValueError as exc:
            raise ConfigError(f"{campo.rotulo}: {exc}") from None
    if campo.tipo == "telefone":
        if not texto:
            return ""
        telefone = normalizar_telefone(texto)
        if telefone is None:
            raise ConfigError(f"{campo.rotulo}: telefone inválido. Use DDD + número, ex.: (11) 99999-8888.")
        return telefone
    if campo.tipo == "longo":
        texto = str(bruto).replace("\r\n", "\n").strip()
        if len(texto) > 4000:
            raise ConfigError(f"{campo.rotulo}: use até 4000 caracteres.")
        return texto
    if campo.tipo == "frase":
        if not texto or len(texto) > 200:
            raise ConfigError(f"{campo.rotulo}: informe um texto de até 200 caracteres.")
        return texto
    if campo.tipo == "texto":
        if not texto or len(texto) > 200 or any(c.isspace() for c in texto):
            raise ConfigError(f"{campo.rotulo}: informe o nome, sem espaços.")
        return texto
    if campo.tipo == "rampa":
        try:
            parse_rampa(texto)
        except ValueError as exc:
            raise ConfigError(f"Rampa inválida: {exc}. Ex.: 1:10,4:20,8:40") from None
        return texto.replace(" ", "")
    try:
        numero = float(texto.replace(",", ".")) if campo.tipo == "decimal" else int(texto)
    except ValueError:
        raise ConfigError(f"{campo.rotulo}: informe um número.") from None
    if (campo.minimo is not None and numero < campo.minimo) or (campo.maximo is not None and numero > campo.maximo):
        raise ConfigError(f"{campo.rotulo}: use um valor entre {campo.minimo:g} e {campo.maximo:g}.")
    return numero


def salvar(db: Session, dados: dict) -> None:
    """dados: chave -> valor do formulário (lista para "dias"). Valida tudo antes de gravar."""
    novos = {chave: _converter(CAMPOS_POR_CHAVE[chave], bruto) for chave, bruto in dados.items() if chave in CAMPOS_POR_CHAVE}
    resultado = {**valores(db), **novos}
    if resultado["intervalo_min_minutos"] > resultado["intervalo_max_minutos"]:
        raise ConfigError("O intervalo mínimo não pode ser maior que o máximo.")
    if resultado["disparo_inicio"] >= resultado["disparo_fim"]:
        raise ConfigError("O início do horário comercial precisa ser antes do fim.")
    if novos.get("notificar_telefone") and db.scalar(
        select(WhatsappNumber.id).where(
            WhatsappNumber.telefone == novos["notificar_telefone"], WhatsappNumber.status != NumeroStatus.REMOVIDO
        )
    ):
        raise ConfigError("O WhatsApp de notificações não pode ser um dos números de prospecção.")
    for chave, valor in novos.items():
        linha = db.scalar(select(Configuracao).where(Configuracao.chave == chave))
        if linha is None:
            db.add(Configuracao(chave=chave, valor=valor))
        else:
            linha.valor = valor
    db.flush()
    logger.info("configurações salvas", extra={"chaves": sorted(novos)})


def restaurar_padroes(db: Session) -> None:
    exigir_cliente(db)
    db.query(Configuracao).delete()
    db.flush()
    logger.info("configurações restauradas para o padrão de fábrica")


# --- Leitura tipada -------------------------------------------------------------


@dataclass(frozen=True)
class RegrasDisparo:
    dias: frozenset[int]
    inicio: time
    fim: time
    intervalo_min_seg: float
    intervalo_max_seg: float
    taxa_minima: float  # em %
    janela_envios: int
    tz: ZoneInfo


def regras_disparo(db: Session) -> RegrasDisparo:
    v = valores(db)
    return RegrasDisparo(
        dias=frozenset(v["disparo_dias"]),
        inicio=time.fromisoformat(v["disparo_inicio"]),
        fim=time.fromisoformat(v["disparo_fim"]),
        intervalo_min_seg=float(v["intervalo_min_minutos"]) * 60,
        intervalo_max_seg=float(v["intervalo_max_minutos"]) * 60,
        taxa_minima=float(v["pausa_taxa_minima"]),
        janela_envios=int(v["pausa_janela_envios"]),
        tz=ZoneInfo(get_settings().timezone),
    )


def rampa(db: Session) -> Rampa:
    return parse_rampa(valores(db)["rampa"])


def rampa_super(db: Session) -> Rampa:
    """A rampa dos números em super prospecção: a mesma, limitada pelo teto por número."""
    teto = int(valores(db)["teto_por_numero"])
    return [(dia, min(limite, teto)) for dia, limite in rampa(db)]


@dataclass(frozen=True)
class RegrasVolume:
    meta_diaria: int  # 0 = sem meta
    teto_por_numero: int
    limite_hora_numero: int
    falhas_seguidas: int  # 0 = não pausa


def regras_volume(db: Session) -> RegrasVolume:
    v = valores(db)
    return RegrasVolume(
        meta_diaria=int(v["meta_diaria"]),
        teto_por_numero=int(v["teto_por_numero"]),
        limite_hora_numero=int(v["limite_hora_numero"]),
        falhas_seguidas=int(v["pausa_falhas_seguidas"]),
    )
