"""Agente de IA que conversa com o lead depois que ele responde a abertura.

Fluxo de uma resposta:
1. a mensagem do lead chega (webhook) e fica no histórico; a resposta é agendada para daqui a
   alguns segundos, para juntar mensagens seguidas;
2. o lead é classificado (segmento e porte) na primeira vez;
3. o modelo recebe o roteiro do segmento/porte, a etapa atual e o histórico, e termina chamando
   uma ferramenta: responder, passar para humano, marcar sem interesse ou opt-out;
4. a resposta sai pelo mesmo número que fez a primeira mensagem.

As funções alteram a sessão mas não fazem commit; quem chama decide.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations import evolution
from app.models import (
    ETAPAS,
    Autor,
    Busca,
    Campanha,
    Direcao,
    Envio,
    EnvioStatus,
    EnvioTipo,
    Etapa,
    Lead,
    LeadStatus,
    Mensagem,
    NumeroStatus,
    Porte,
    Segmento,
    WebhookEvent,
    WhatsappNumber,
)
from app.services import agenda, config_sistema, ia, roteiros
from app.services.agenda import AgendaError
from app.services.ia import IAError
from app.services.idempotency import register_event
from app.services.telefone import para_evolution

logger = logging.getLogger(__name__)

# Com estes status a IA não fala mais com o lead.
STATUS_SEM_IA = {LeadStatus.OPT_OUT, LeadStatus.HUMANO_ASSUMIU, LeadStatus.SEM_INTERESSE, LeadStatus.SEM_RESPOSTA}
MAX_PASSOS = 6
ROTULO_PORTE = {Porte.PEQUENO: "pequeno", Porte.MEDIO: "médio", Porte.GRANDE: "grande"}


def pode_responder(lead: Lead) -> bool:
    return lead.ia_ativa and lead.status not in STATUS_SEM_IA and lead.numero_id is not None


# --- Envio e registro -------------------------------------------------------------


def marcar_como_do_sistema(db: Session, message_id: str | None) -> None:
    """Guarda o id de tudo que o sistema manda, para separar das mensagens de um humano."""
    if message_id:
        register_event(db, "envio-sistema", message_id)


def eh_do_sistema(db: Session, message_id: str) -> bool:
    return bool(
        db.scalar(select(WebhookEvent.id).where(WebhookEvent.origem == "envio-sistema", WebhookEvent.chave == message_id))
        or db.scalar(select(Envio.id).where(Envio.evolution_message_id == message_id))
    )


def enviar_ao_lead(db: Session, lead: Lead, texto: str) -> Envio:
    numero = db.get(WhatsappNumber, lead.numero_id)
    envio = Envio(lead_id=lead.id, campanha_id=lead.campanha_id, numero_id=numero.id, tipo=EnvioTipo.IA, texto=texto, status=EnvioStatus.PENDENTE)
    db.add(envio)
    db.flush()  # o texto fica registrado antes do envio, caso o webhook do próprio envio chegue primeiro
    # "digitando…" proporcional ao tamanho, entre 2 e 8 segundos
    digitando_ms = min(8000, max(2000, len(texto) * 40))
    resposta = evolution.get_client(numero.server).send_text(numero.instance_name, para_evolution(lead.telefone), texto, delay_ms=digitando_ms)
    envio.status = EnvioStatus.ENVIADO
    envio.enviado_em = datetime.now(timezone.utc)
    envio.evolution_message_id = (resposta.get("key") or {}).get("id")
    marcar_como_do_sistema(db, envio.evolution_message_id)
    db.add(Mensagem(lead_id=lead.id, numero_id=numero.id, direcao=Direcao.SAIDA, autor=Autor.IA, texto=texto, evolution_message_id=envio.evolution_message_id))
    logger.info("resposta da IA enviada", extra={"lead_id": lead.id, "numero_id": numero.id, "etapa": lead.etapa})
    return envio


def passar_para_humano(db: Session, lead: Lead, motivo: str) -> None:
    """A IA para de responder este lead e o time é sinalizado no Chatwoot (label "humano")."""
    lead.ia_ativa = False
    lead.status = LeadStatus.HUMANO_ASSUMIU
    lead.handoff_motivo = motivo[:300]
    lead.responder_em = None
    logger.info("lead passado para humano", extra={"lead_id": lead.id, "motivo": motivo})


def devolver_para_ia(db: Session, lead: Lead) -> None:
    lead.ia_ativa = True
    lead.handoff_motivo = None
    if lead.status == LeadStatus.HUMANO_ASSUMIU:
        lead.status = LeadStatus.EM_CONVERSA
    logger.info("lead devolvido para a IA", extra={"lead_id": lead.id})


# --- Classificação --------------------------------------------------------------------


def porte_por_avaliacoes(avaliacoes: int | None) -> str:
    """Estimativa grosseira quando a IA não responde: o nº de avaliações no Google acompanha o movimento."""
    if (avaliacoes or 0) >= 500:
        return Porte.GRANDE
    if (avaliacoes or 0) >= 100:
        return Porte.MEDIO
    return Porte.PEQUENO


_FERRAMENTA_CLASSIFICAR = {
    "name": "classificar_lead",
    "description": "Registra o segmento e o porte estimado da empresa.",
    "input_schema": {
        "type": "object",
        "properties": {
            "segmento": {"type": "string", "description": "Um dos segmentos da lista, exatamente como escrito, ou \"nenhum\"."},
            "porte": {"type": "string", "enum": ["pequeno", "medio", "grande"]},
        },
        "required": ["segmento", "porte"],
    },
}


def classificar(db: Session, lead: Lead) -> None:
    """Define segmento (palavras-chave; a IA só quando não bate) e porte (IA; se falhar, pelas avaliações)."""
    busca = db.get(Busca, lead.busca_id) if lead.busca_id else None
    if not lead.segmento:
        achado = roteiros.segmento_por_palavras(db, lead.categoria, busca.termo if busca else None, lead.nome)
        lead.segmento = achado.nome if achado else None
    if lead.porte and lead.segmento:
        return
    nomes = list(db.scalars(select(Segmento.nome).order_by(Segmento.id)))
    dados = {
        "nome": lead.nome,
        "categoria": lead.categoria,
        "cidade": lead.cidade,
        "avaliacoes_no_google": lead.avaliacoes,
        "nota": lead.nota,
        "tem_site": bool(lead.site),
        "termo_da_busca": busca.termo if busca else None,
    }
    instrucao = (
        "Classifique a empresa abaixo para uma prospecção de produtos de limpeza profissional.\n"
        f"Segmentos possíveis: {', '.join(nomes) or 'nenhum cadastrado'}.\n"
        + (f"O segmento já foi definido como \"{lead.segmento}\"; repita-o.\n" if lead.segmento else "")
        + "Porte: pequeno (operação familiar ou uma unidade simples), medio (equipe maior, mais movimento), "
        "grande (rede, várias unidades, indústria ou muito movimento). O nº de avaliações no Google é só uma pista: "
        "pousadas, restaurantes e academias pequenas costumam ter muitas avaliações. Na dúvida, escolha pequeno.\n"
        "Responda chamando a ferramenta classificar_lead.\n\n"
        f"Empresa: {json.dumps(dados, ensure_ascii=False)}"
    )
    try:
        response = ia.chamar(
            model=ia.modelo(db), max_tokens=500, tools=[_FERRAMENTA_CLASSIFICAR], messages=[{"role": "user", "content": instrucao}]
        )
        resultado = next((b.input for b in response.content if b.type == "tool_use" and b.name == "classificar_lead"), None) or {}
    except IAError as exc:
        logger.warning("classificação pela IA falhou; usando as avaliações", extra={"lead_id": lead.id, "erro": str(exc)})
        resultado = {}
    if not lead.segmento and resultado.get("segmento") in nomes:
        lead.segmento = resultado["segmento"]
    lead.porte = resultado.get("porte") if resultado.get("porte") in set(Porte) else porte_por_avaliacoes(lead.avaliacoes)
    logger.info("lead classificado", extra={"lead_id": lead.id, "segmento": lead.segmento, "porte": lead.porte})


# --- Conversa -------------------------------------------------------------------------


FERRAMENTAS = [
    {
        "name": "responder",
        "description": "Envia suas mensagens ao lead, na ordem. Use sempre que for escrever para ele.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mensagens": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Uma mensagem por item (curtas, para WhatsApp, sem markdown). Saem com poucos segundos entre elas.",
                },
                "etapa": {
                    "type": "string",
                    "enum": [*ETAPAS, "conversa"],
                    "description": "Última etapa do roteiro entregue nestas mensagens, ou \"conversa\" se só respondeu uma dúvida sem avançar.",
                },
            },
            "required": ["mensagens", "etapa"],
        },
    },
    {
        "name": "consultar_horarios",
        "description": "Lista horários livres para a reunião. Consulte antes de oferecer horários.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "agendar_reuniao",
        "description": "Marca a reunião no horário que o lead escolheu (use o valor 'inicio' devolvido por consultar_horarios).",
        "input_schema": {
            "type": "object",
            "properties": {
                "inicio": {"type": "string", "description": "Data e hora em ISO 8601, como veio de consultar_horarios."},
                "observacoes": {"type": "string", "description": "O que o vendedor precisa saber (dor, porte, interesse)."},
            },
            "required": ["inicio"],
        },
    },
    {
        "name": "atualizar_porte",
        "description": "Corrige o porte da empresa quando o lead der pistas (nº de unidades, funcionários, volume).",
        "input_schema": {
            "type": "object",
            "properties": {"porte": {"type": "string", "enum": list(Porte)}, "motivo": {"type": "string"}},
            "required": ["porte", "motivo"],
        },
    },
    {
        "name": "passar_para_humano",
        "description": "Passa a conversa para alguém do time e para de responder. Use nos casos da seção 'Quando passar para humano'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {"type": "string"},
                "mensagem_ao_lead": {"type": "string", "description": "Opcional: aviso curto de que um colega vai continuar."},
            },
            "required": ["motivo"],
        },
    },
    {
        "name": "marcar_sem_interesse",
        "description": "O lead deixou claro que não tem interesse. Encerra a conversa com educação.",
        "input_schema": {
            "type": "object",
            "properties": {"motivo": {"type": "string"}, "mensagem_de_despedida": {"type": "string"}},
            "required": ["motivo"],
        },
    },
    {
        "name": "registrar_opt_out",
        "description": "O lead pediu para não ser mais contatado. Não envie mais nada depois disso.",
        "input_schema": {"type": "object", "properties": {"motivo": {"type": "string"}}, "required": ["motivo"]},
    },
]


def _historico(db: Session, lead: Lead) -> tuple[str, list[dict]]:
    """(texto da abertura, mensagens no formato da API a partir da primeira resposta do lead)."""
    mensagens = db.scalars(select(Mensagem).where(Mensagem.lead_id == lead.id).order_by(Mensagem.id)).all()
    abertura = next((m.texto for m in mensagens if m.direcao == Direcao.SAIDA), "")
    conversa: list[dict] = []
    comecou = False
    for m in mensagens:
        if m.direcao == Direcao.ENTRADA:
            comecou = True
        if not comecou:
            continue
        papel = "user" if m.direcao == Direcao.ENTRADA else "assistant"
        texto = m.texto if m.tipo == "texto" else f"[{m.tipo}]"
        if m.autor == Autor.HUMANO:
            texto = f"(mensagem de um colega do time) {texto}"
        if conversa and conversa[-1]["role"] == papel:
            conversa[-1]["content"] += "\n" + texto
        else:
            conversa.append({"role": papel, "content": texto})
    return abertura, conversa


def instrucao_da_vez(etapa: str, pitch_em_sequencia: bool) -> str:
    """O que a IA deve entregar nesta resposta, dita de forma explícita (o modelo tende a adiantar etapas)."""
    if etapa == Etapa.ATENCAO:
        return ("NESTA RESPOSTA: entregue só a Atenção, em UMA mensagem, e pare para o lead responder. "
                "Não fale ainda de produto, economia nem convite (etapa = atencao).")
    if etapa == Etapa.INTERESSE and pitch_em_sequencia:
        return ("NESTA RESPOSTA: é a hora do pitch. Entregue Interesse, Desejo e Ação de uma vez, em TRÊS mensagens curtas "
                "na lista `mensagens`, nessa ordem; a última termina com o convite/pergunta da Ação (etapa = acao). "
                "Se o lead fez uma pergunta, responda-a no começo da primeira mensagem.")
    if etapa == Etapa.ACAO:
        return ("NESTA RESPOSTA: o roteiro já chegou à Ação. Conduza para a reunião: tire dúvidas, consulte os horários "
                "e agende. Uma mensagem por resposta do lead.")
    return f"NESTA RESPOSTA: entregue só a etapa {roteiros.ROTULO_ETAPA.get(etapa, etapa)}, em uma mensagem (uma etapa por resposta do lead)."


def montar_sistema(db: Session, lead: Lead, abertura: str) -> str:
    v = config_sistema.valores(db)
    roteiro = roteiros.roteiro_do_lead(db, lead)
    campanha = db.get(Campanha, lead.campanha_id) if lead.campanha_id else None
    etapa = lead.etapa or Etapa.ATENCAO
    em_sequencia = bool(v["pitch_em_sequencia"]) and etapa == Etapa.INTERESSE
    agora = datetime.now(timezone.utc).astimezone(agenda.regras_padrao().tz)

    partes = [
        f"Você é {v['atendente_nome']}, da {v['empresa_nome']}, e conversa pelo WhatsApp com um possível cliente.",
        f"Hoje é {agenda.DIAS_SEMANA[agora.weekday()]}, {agora:%d/%m/%Y}, {agora:%H:%M} (horário de Brasília).",
        f"Empresa do lead: {lead.nome}" + (f" — {lead.categoria}" if lead.categoria else "") + (f", {lead.cidade}" if lead.cidade else "") + ".",
        f"Você mandou a primeira mensagem: \"{abertura}\". O lead respondeu e a conversa segue a partir daí.",
    ]
    if roteiro:
        linhas = []
        for nome_etapa in ETAPAS:
            texto = roteiro["etapas"].get(nome_etapa)
            if texto:
                if em_sequencia and nome_etapa in (Etapa.INTERESSE, Etapa.DESEJO, Etapa.ACAO):
                    marca = "  ← PRÓXIMAS (enviar juntas)"
                else:
                    marca = "  ← PRÓXIMA" if nome_etapa == etapa else ""
                linhas.append(f"- {roteiros.ROTULO_ETAPA[nome_etapa]}{marca}: {texto}")
        partes.append(
            f"\nRoteiro para {roteiro['segmento']}, porte {ROTULO_PORTE.get(roteiro['porte'], roteiro['porte'])} "
            f"(foco: {roteiro['foco'] or 'geral'}). Siga as etapas na ordem:\n" + "\n".join(linhas)
        )
        partes.append("\n" + instrucao_da_vez(etapa, bool(v["pitch_em_sequencia"])))
    elif campanha and campanha.oferta:
        partes.append(f"\nOferta: {campanha.oferta}. Entenda a necessidade do lead e convide para uma conversa com o time.")
    if campanha and campanha.prompt_agente:
        partes.append(f"\nInstruções da campanha:\n{campanha.prompt_agente}")
    partes.append(
        f"""
Porte: o roteiro acima é do porte estimado ({ROTULO_PORTE.get(lead.porte, lead.porte or "pequeno")}). Se o lead disser algo que mostre
outro porte (ex.: "aqui é pequeno", nº de quartos, unidades ou funcionários), chame atualizar_porte ANTES de responder e siga o roteiro novo.

Como escrever:
- Use o roteiro como base e adapte ao que o lead disse. Não copie o texto inteiro: o WhatsApp pede mensagens curtas (até 3 frases).
- Português do Brasil, tom humano e cordial, sem markdown, sem listas, no máximo um emoji.
- Uma pergunta por vez. Se o lead fizer uma pergunta, responda primeiro e só depois avance.
- Apresente-se uma vez só: se a abertura já disse seu nome, não repita a apresentação inteira. Ao falar de economia use "{v['economia_texto']}"; nunca invente outros números, preços, prazos ou certificações.
- Se o lead der pistas do tamanho da empresa (unidades, funcionários, volume), chame atualizar_porte antes de responder.
- Se perguntarem se você é um robô, seja honesta: diga que é a assistente virtual da {v['empresa_nome']} e ofereça falar com alguém do time.

Etapa Ação e agendamento:
- Para marcar a conversa, chame consultar_horarios e ofereça as duas sugestões. Quando o lead escolher, chame agendar_reuniao e confirme dia, hora e o link.

Quando passar para humano (chame passar_para_humano):
- pedido de preço detalhado, tabela ou orçamento;
- irritação, reclamação ou ameaça;
- dúvida técnica ou assunto fora do escopo que você não sabe responder com segurança;
- pedido para falar com uma pessoa.

Encerrar:
- "Não tenho interesse" claro e definitivo: chame marcar_sem_interesse (com uma despedida curta e educada).
- Pedido para não ser mais contatado: chame registrar_opt_out.

Termine SEMPRE chamando uma ferramenta: responder (para escrever ao lead), passar_para_humano, marcar_sem_interesse ou registrar_opt_out."""
    )
    return "\n".join(partes)


def _executar_ferramenta(db: Session, lead: Lead, nome: str, entrada: dict) -> tuple[str, bool]:
    """Executa uma ferramenta de consulta/ação. Retorna (resultado para o modelo, terminou a vez)."""
    if nome == "consultar_horarios":
        livres = agenda.horarios_livres(db)
        if not livres:
            return "Não há horários livres na agenda. Passe para humano para combinar o horário.", False
        sugestoes = agenda.sugerir_horarios(livres)
        lista = [{"inicio": h.inicio.isoformat(), "quando": agenda.formatar_horario(h.inicio)} for h in livres[:8]]
        return json.dumps(
            {"sugestoes": [agenda.formatar_horario(h.inicio) for h in sugestoes], "horarios": lista}, ensure_ascii=False
        ), False
    if nome == "agendar_reuniao":
        try:
            inicio = datetime.fromisoformat(entrada.get("inicio", ""))
            reuniao = agenda.agendar(
                db, inicio, lead_nome=lead.nome, lead_telefone=lead.telefone, lead_id=lead.id,
                observacoes=(entrada.get("observacoes") or "Agendado pela IA")[:1000],
            )
        except (ValueError, AgendaError) as exc:
            return f"Não foi possível agendar: {exc}. Consulte os horários de novo e ofereça outros.", False
        lead.status = LeadStatus.REUNIAO_AGENDADA
        db.info.setdefault("reunioes_novas", []).append(reuniao.id)
        return json.dumps(
            {"agendado": agenda.formatar_horario(reuniao.inicio), "link": reuniao.link or "o vendedor envia o link antes da reunião"},
            ensure_ascii=False,
        ), False
    if nome == "atualizar_porte":
        if entrada.get("porte") in set(Porte):
            lead.porte = entrada["porte"]
            logger.info("porte atualizado pela IA", extra={"lead_id": lead.id, "porte": lead.porte, "motivo": entrada.get("motivo")})
            novo = roteiros.roteiro_do_lead(db, lead)
            if novo:
                return "Porte atualizado. Roteiro deste porte:\n" + "\n".join(
                    f"- {roteiros.ROTULO_ETAPA[e]}: {t}" for e, t in novo["etapas"].items()
                ), False
            return "Porte atualizado.", False
        return "Porte inválido.", False
    return f"Ferramenta desconhecida: {nome}", False


def _avancar_etapa(lead: Lead, etapa: str) -> None:
    if etapa not in ETAPAS:
        return
    i = ETAPAS.index(etapa)
    lead.etapa = ETAPAS[min(i + 1, len(ETAPAS) - 1)]


def conversar(db: Session, lead: Lead) -> str:
    """Uma vez da IA. Retorna o que aconteceu: respondeu, humano, sem_interesse, opt_out, nada."""
    if not pode_responder(lead):
        return "nada"
    numero = db.get(WhatsappNumber, lead.numero_id)
    if numero is None or numero.status not in (NumeroStatus.CONECTADO, NumeroStatus.PAUSADO):
        logger.warning("número do lead indisponível; resposta adiada", extra={"lead_id": lead.id})
        return "nada"
    if not lead.porte or not lead.segmento:
        classificar(db, lead)
    abertura, mensagens = _historico(db, lead)
    if not mensagens or mensagens[-1]["role"] != "user":
        return "nada"  # nada novo do lead
    sistema = montar_sistema(db, lead, abertura)

    for _ in range(MAX_PASSOS):
        try:
            response = ia.chamar(model=ia.modelo(db), max_tokens=2000, system=sistema, tools=FERRAMENTAS, messages=mensagens)
        except IAError as exc:
            logger.error("IA indisponível; lead passado para humano", extra={"lead_id": lead.id, "erro": str(exc)})
            passar_para_humano(db, lead, f"IA indisponível: {exc}")
            return "humano"
        usos = [b for b in response.content if b.type == "tool_use"]
        if not usos:
            texto = "".join(b.text for b in response.content if b.type == "text").strip()
            if not texto:
                passar_para_humano(db, lead, "a IA não conseguiu formular uma resposta")
                return "humano"
            enviar_ao_lead(db, lead, texto)
            _avancar_etapa(lead, lead.etapa or Etapa.ATENCAO)
            _depois_de_responder(lead)
            return "respondeu"

        terminais = {"responder", "passar_para_humano", "marcar_sem_interesse", "registrar_opt_out"}
        final = next((u for u in usos if u.name in terminais), None)
        if final is not None:
            return _finalizar(db, lead, final.name, final.input or {})

        resultados = []
        for uso in usos:
            conteudo, _ = _executar_ferramenta(db, lead, uso.name, uso.input or {})
            resultados.append({"type": "tool_result", "tool_use_id": uso.id, "content": conteudo})
        mensagens = mensagens + [{"role": "assistant", "content": response.content}, {"role": "user", "content": resultados}]

    passar_para_humano(db, lead, "a IA não concluiu a resposta")
    return "humano"


def _depois_de_responder(lead: Lead) -> None:
    if lead.status in (LeadStatus.ENVIADO, LeadStatus.RESPONDEU):
        lead.status = LeadStatus.EM_CONVERSA


def _finalizar(db: Session, lead: Lead, nome: str, entrada: dict) -> str:
    from app.services.mensagens import adicionar_blacklist

    if nome == "responder":
        mensagens = entrada.get("mensagens")
        if isinstance(mensagens, str):
            mensagens = [mensagens]
        if not mensagens and entrada.get("mensagem"):  # formato antigo, com uma mensagem só
            mensagens = [entrada["mensagem"]]
        mensagens = [m.strip() for m in (mensagens or []) if isinstance(m, str) and m.strip()][:4]
        if not mensagens:
            passar_para_humano(db, lead, "a IA devolveu uma resposta vazia")
            return "humano"
        etapa = entrada.get("etapa", "conversa")
        if (lead.etapa or Etapa.ATENCAO) == Etapa.ATENCAO and etapa in ETAPAS:
            # Trava: na vez da Atenção a IA não pode adiantar o pitch. Se ela disse que foi além da Atenção,
            # sai só a primeira mensagem; se ficou na Atenção (às vezes em duas partes), saem as duas.
            mensagens = mensagens[:2] if etapa == Etapa.ATENCAO else mensagens[:1]
            etapa = Etapa.ATENCAO
        for texto in mensagens:
            # O "digitando…" de cada envio (2 a 8 s) separa as mensagens, como alguém escrevendo.
            enviar_ao_lead(db, lead, texto)
        _avancar_etapa(lead, etapa)
        _depois_de_responder(lead)
        return "respondeu"
    if nome == "passar_para_humano":
        if (entrada.get("mensagem_ao_lead") or "").strip():
            enviar_ao_lead(db, lead, entrada["mensagem_ao_lead"].strip())
        passar_para_humano(db, lead, entrada.get("motivo") or "pedido da IA")
        return "humano"
    if nome == "marcar_sem_interesse":
        if (entrada.get("mensagem_de_despedida") or "").strip():
            enviar_ao_lead(db, lead, entrada["mensagem_de_despedida"].strip())
        lead.status = LeadStatus.SEM_INTERESSE
        lead.ia_ativa = False
        lead.handoff_motivo = (entrada.get("motivo") or "")[:300] or None
        logger.info("lead sem interesse", extra={"lead_id": lead.id})
        return "sem_interesse"
    # registrar_opt_out
    lead.status = LeadStatus.OPT_OUT
    lead.ia_ativa = False
    adicionar_blacklist(db, lead.telefone, f"opt-out pela IA: {entrada.get('motivo', '')}"[:200])
    logger.info("opt-out registrado pela IA", extra={"lead_id": lead.id})
    return "opt_out"
