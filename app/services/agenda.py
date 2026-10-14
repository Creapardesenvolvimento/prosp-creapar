"""Agenda interna: horários livres dos vendedores e reserva de reuniões.

As funções alteram a sessão mas não fazem commit: quem chama decide.
"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.integrations import evolution
from app.models import Reuniao, ReuniaoStatus, Vendedor, VendedorBloqueio, VendedorHorario
from app.services import config_sistema
from app.services.numeros import numeros_no_rodizio
from app.services.telefone import normalizar_telefone, para_evolution

logger = logging.getLogger(__name__)

DIAS_SEMANA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
Intervalo = tuple[datetime, datetime]


class AgendaError(Exception):
    pass


class HorarioIndisponivel(AgendaError):
    pass


@dataclass(frozen=True)
class Regras:
    duracao: timedelta
    buffer: timedelta
    antecedencia: timedelta
    dias_a_frente: int
    tz: ZoneInfo


def regras_padrao() -> Regras:
    s = get_settings()
    return Regras(
        duracao=timedelta(minutes=s.reuniao_minutos),
        buffer=timedelta(minutes=s.reuniao_buffer_minutos),
        antecedencia=timedelta(hours=s.agenda_antecedencia_horas),
        dias_a_frente=s.agenda_dias_a_frente,
        tz=ZoneInfo(s.timezone),
    )


def regras_do_sistema(db: Session) -> Regras:
    """Regras da agenda com os valores ajustados no painel (Configurações)."""
    v = config_sistema.valores(db)
    return Regras(
        duracao=timedelta(minutes=int(v["reuniao_minutos"])),
        buffer=timedelta(minutes=int(v["reuniao_buffer_minutos"])),
        antecedencia=timedelta(hours=int(v["agenda_antecedencia_horas"])),
        dias_a_frente=int(v["agenda_dias_a_frente"]),
        tz=ZoneInfo(get_settings().timezone),
    )


def como_utc(valor: datetime) -> datetime:
    """SQLite devolve datetimes sem fuso; gravamos sempre em UTC."""
    return valor.replace(tzinfo=timezone.utc) if valor.tzinfo is None else valor


def utc(valor: datetime) -> datetime:
    """Converte para UTC antes de gravar ou filtrar no banco (o SQLite descarta o fuso sem converter)."""
    return como_utc(valor).astimezone(timezone.utc)


@dataclass
class AgendaVendedor:
    vendedor_id: int
    horarios: list[tuple[int, time, time]]
    bloqueios: list[Intervalo] = field(default_factory=list)
    reunioes: list[Intervalo] = field(default_factory=list)


@dataclass(frozen=True)
class Horario:
    inicio: datetime
    fim: datetime
    vendedores: tuple[int, ...]


def _sobrepoe(a_inicio: datetime, a_fim: datetime, b_inicio: datetime, b_fim: datetime) -> bool:
    return a_inicio < b_fim and b_inicio < a_fim


def _livre(agenda: AgendaVendedor, inicio: datetime, fim: datetime, buffer: timedelta) -> bool:
    if any(_sobrepoe(inicio, fim, b_inicio, b_fim) for b_inicio, b_fim in agenda.bloqueios):
        return False
    return not any(_sobrepoe(inicio, fim, r_inicio - buffer, r_fim + buffer) for r_inicio, r_fim in agenda.reunioes)


def calcular_horarios(agendas: list[AgendaVendedor], agora: datetime, regras: Regras) -> list[Horario]:
    """Horários em que pelo menos um vendedor pode atender, em ordem, no fuso das regras."""
    minimo = como_utc(agora) + regras.antecedencia
    hoje = como_utc(agora).astimezone(regras.tz).date()
    livres: dict[datetime, set[int]] = defaultdict(set)
    for offset in range(regras.dias_a_frente + 1):
        dia = hoje + timedelta(days=offset)
        for agenda in agendas:
            for dia_semana, faixa_inicio, faixa_fim in agenda.horarios:
                if dia_semana != dia.weekday():
                    continue
                inicio = datetime.combine(dia, faixa_inicio, tzinfo=regras.tz)
                limite = datetime.combine(dia, faixa_fim, tzinfo=regras.tz)
                while inicio + regras.duracao <= limite:
                    fim = inicio + regras.duracao
                    if inicio >= minimo and _livre(agenda, inicio, fim, regras.buffer):
                        livres[inicio].add(agenda.vendedor_id)
                    inicio = fim
    return [Horario(i, i + regras.duracao, tuple(sorted(v))) for i, v in sorted(livres.items())]


def sugerir_horarios(horarios: list[Horario], quantidade: int = 2) -> list[Horario]:
    """Escolhe opções espalhadas: a primeira livre e as seguintes em outro dia ou 3h+ depois."""
    escolhidos: list[Horario] = []
    for horario in horarios:
        if len(escolhidos) == quantidade:
            break
        if not escolhidos:
            escolhidos.append(horario)
            continue
        ultimo = escolhidos[-1]
        if horario.inicio.date() != ultimo.inicio.date() or horario.inicio - ultimo.inicio >= timedelta(hours=3):
            escolhidos.append(horario)
    for horario in horarios:  # poucos horários: completa com os que sobraram
        if len(escolhidos) == quantidade:
            break
        if horario not in escolhidos:
            escolhidos.append(horario)
    return sorted(escolhidos, key=lambda h: h.inicio)


def carregar_agendas(db: Session, inicio: datetime, fim: datetime) -> list[AgendaVendedor]:
    vendedores = db.scalars(
        select(Vendedor).where(Vendedor.ativo).options(selectinload(Vendedor.horarios)).order_by(Vendedor.id)
    ).all()
    agendas = {
        v.id: AgendaVendedor(v.id, [(h.dia_semana, h.inicio, h.fim) for h in v.horarios]) for v in vendedores
    }
    bloqueios = db.scalars(
        select(VendedorBloqueio).where(
            VendedorBloqueio.vendedor_id.in_(agendas), VendedorBloqueio.inicio < fim, VendedorBloqueio.fim > inicio
        )
    )
    for b in bloqueios:
        agendas[b.vendedor_id].bloqueios.append((como_utc(b.inicio), como_utc(b.fim)))
    reunioes = db.scalars(
        select(Reuniao).where(
            Reuniao.vendedor_id.in_(agendas),
            Reuniao.status == ReuniaoStatus.AGENDADA,
            Reuniao.inicio < fim,
            Reuniao.fim > inicio,
        )
    )
    for r in reunioes:
        agendas[r.vendedor_id].reunioes.append((como_utc(r.inicio), como_utc(r.fim)))
    return list(agendas.values())


def horarios_livres(db: Session, agora: datetime | None = None, regras: Regras | None = None) -> list[Horario]:
    regras = regras or regras_do_sistema(db)
    agora = agora or datetime.now(timezone.utc)
    janela_fim = agora + timedelta(days=regras.dias_a_frente + 2)
    # A folga cobre reuniões que começam antes da janela mas cujo intervalo invade o primeiro horário.
    folga = regras.duracao + regras.buffer
    return calcular_horarios(carregar_agendas(db, utc(agora - folga), utc(janela_fim + folga)), agora, regras)


def _menos_ocupado(db: Session, vendedor_ids: tuple[int, ...], inicio: datetime, regras: Regras) -> int:
    """Vendedor com menos reuniões agendadas na semana (seg–dom) do horário; empate: menor id."""
    local = inicio.astimezone(regras.tz)
    semana_inicio = datetime.combine(local.date() - timedelta(days=local.weekday()), time(0), tzinfo=regras.tz)
    semana_fim = semana_inicio + timedelta(days=7)
    contagem = dict(
        db.execute(
            select(Reuniao.vendedor_id, func.count())
            .where(
                Reuniao.vendedor_id.in_(vendedor_ids),
                Reuniao.status == ReuniaoStatus.AGENDADA,
                Reuniao.inicio >= utc(semana_inicio),
                Reuniao.inicio < utc(semana_fim),
            )
            .group_by(Reuniao.vendedor_id)
        ).all()
    )
    return min(vendedor_ids, key=lambda vid: (contagem.get(vid, 0), vid))


def agendar(
    db: Session,
    inicio: datetime,
    *,
    lead_nome: str,
    lead_telefone: str | None = None,
    lead_id: int | None = None,
    observacoes: str = "",
    agora: datetime | None = None,
    regras: Regras | None = None,
) -> Reuniao:
    """Reserva o horário para um vendedor livre. Levanta HorarioIndisponivel se ninguém puder."""
    regras = regras or regras_do_sistema(db)
    inicio = como_utc(inicio)
    # Trava os vendedores (no Postgres) para duas reservas simultâneas não pegarem o mesmo horário.
    db.scalars(select(Vendedor.id).where(Vendedor.ativo).order_by(Vendedor.id).with_for_update()).all()

    horario = next((h for h in horarios_livres(db, agora, regras) if h.inicio == inicio), None)
    if horario is None:
        raise HorarioIndisponivel("Esse horário não está mais disponível.")

    vendedor = db.get(Vendedor, _menos_ocupado(db, horario.vendedores, horario.inicio, regras))
    reuniao = Reuniao(
        vendedor=vendedor,
        lead_id=lead_id,
        lead_nome=lead_nome.strip(),
        lead_telefone=normalizar_telefone(lead_telefone) or lead_telefone,
        inicio=utc(horario.inicio),
        fim=utc(horario.fim),
        status=ReuniaoStatus.AGENDADA,
        link=vendedor.link_reuniao,
        observacoes=observacoes,
    )
    db.add(reuniao)
    db.flush()
    logger.info(
        "reunião agendada",
        extra={"reuniao_id": reuniao.id, "vendedor_id": vendedor.id, "inicio": horario.inicio.isoformat(), "lead_id": lead_id},
    )
    return reuniao


def mudar_status(db: Session, reuniao: Reuniao, status: ReuniaoStatus) -> None:
    if reuniao.status != ReuniaoStatus.AGENDADA:
        raise AgendaError("Só é possível alterar uma reunião que está agendada.")
    reuniao.status = status
    logger.info("reunião atualizada", extra={"reuniao_id": reuniao.id, "status": status})


def formatar_horario(inicio: datetime, regras: Regras | None = None) -> str:
    """ "quinta, 16/10 às 14:30" """
    regras = regras or regras_padrao()
    local = como_utc(inicio).astimezone(regras.tz)
    return f"{DIAS_SEMANA[local.weekday()]}, {local:%d/%m} às {local:%H:%M}"


# --- Cadastro de vendedores ---------------------------------------------------

_FAIXA = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_faixas(texto: str) -> list[tuple[time, time]]:
    """ "09:00-12:00, 14:00-18:00" -> [(09:00, 12:00), (14:00, 18:00)] """
    faixas = []
    for parte in filter(str.strip, texto.split(",")):
        m = _FAIXA.match(parte)
        if not m:
            raise AgendaError(f"Faixa inválida: '{parte.strip()}'. Use o formato 09:00-12:00.")
        try:
            inicio, fim = time(int(m[1]), int(m[2])), time(int(m[3]), int(m[4]))
        except ValueError:
            raise AgendaError(f"Horário inválido em '{parte.strip()}'.") from None
        if inicio >= fim:
            raise AgendaError(f"Na faixa '{parte.strip()}' o início precisa ser antes do fim.")
        faixas.append((inicio, fim))
    faixas.sort()
    for (_, fim_anterior), (inicio, _) in zip(faixas, faixas[1:]):
        if inicio < fim_anterior:
            raise AgendaError("Há faixas sobrepostas no mesmo dia.")
    return faixas


def formatar_faixas(vendedor: Vendedor, dia_semana: int) -> str:
    return ", ".join(f"{h.inicio:%H:%M}-{h.fim:%H:%M}" for h in vendedor.horarios if h.dia_semana == dia_semana)


def salvar_vendedor(
    db: Session,
    vendedor: Vendedor | None,
    *,
    nome: str,
    telefone: str,
    email: str,
    link_reuniao: str,
    ativo: bool,
    faixas_por_dia: dict[int, str],
) -> Vendedor:
    nome = nome.strip()
    if not nome:
        raise AgendaError("Informe o nome do vendedor.")
    telefone_e164 = normalizar_telefone(telefone) if telefone.strip() else None
    if telefone.strip() and telefone_e164 is None:
        raise AgendaError("Telefone inválido. Use DDD + número, ex.: (11) 99999-8888.")
    horarios = [
        VendedorHorario(dia_semana=dia, inicio=inicio, fim=fim)
        for dia in range(7)
        for inicio, fim in parse_faixas(faixas_por_dia.get(dia, ""))
    ]

    if vendedor is None:
        vendedor = Vendedor()
        db.add(vendedor)
    vendedor.nome = nome
    vendedor.telefone = telefone_e164
    vendedor.email = email.strip() or None
    vendedor.link_reuniao = link_reuniao.strip() or None
    vendedor.ativo = ativo
    vendedor.horarios = horarios
    db.flush()
    logger.info("vendedor salvo", extra={"vendedor_id": vendedor.id, "faixas": len(horarios)})
    return vendedor


def adicionar_bloqueio(
    db: Session, vendedor: Vendedor, inicio_local: str, fim_local: str, motivo: str, regras: Regras | None = None
) -> VendedorBloqueio:
    """Datas vêm do <input type="datetime-local"> (sem fuso), no fuso da agenda."""
    regras = regras or regras_padrao()
    try:
        inicio = datetime.fromisoformat(inicio_local).replace(tzinfo=regras.tz)
        fim = datetime.fromisoformat(fim_local).replace(tzinfo=regras.tz)
    except ValueError:
        raise AgendaError("Informe início e fim do bloqueio.") from None
    if inicio >= fim:
        raise AgendaError("O fim do bloqueio precisa ser depois do início.")
    bloqueio = VendedorBloqueio(vendedor=vendedor, inicio=utc(inicio), fim=utc(fim), motivo=motivo.strip())
    db.add(bloqueio)
    db.flush()
    return bloqueio


# --- Aviso ao vendedor --------------------------------------------------------


def mensagem_aviso(reuniao: Reuniao) -> str:
    linhas = [
        "📅 *Nova reunião agendada*",
        f"Lead: {reuniao.lead_nome}",
    ]
    if reuniao.lead_telefone:
        linhas.append(f"WhatsApp: {reuniao.lead_telefone}")
    linhas.append(f"Quando: {formatar_horario(reuniao.inicio)}")
    if reuniao.link:
        linhas.append(f"Link: {reuniao.link}")
    if reuniao.observacoes:
        linhas.append(f"Obs.: {reuniao.observacoes}")
    return "\n".join(linhas)


def mensagem_cancelamento(reuniao: Reuniao) -> str:
    return "\n".join(
        [
            "❌ *Reunião cancelada*",
            f"Lead: {reuniao.lead_nome}",
            f"Era: {formatar_horario(reuniao.inicio)}",
            "O horário voltou a ficar livre na agenda.",
        ]
    )


def _enviar_ao_vendedor(db: Session, reuniao: Reuniao, texto: str, evento: str) -> str | None:
    """Manda o texto ao WhatsApp do vendedor pelo número conectado com menos envios. Retorna o id da mensagem."""
    numeros = sorted(numeros_no_rodizio(db), key=lambda n: n.envios_hoje)
    if not numeros:
        logger.warning("nenhum número conectado para avisar o vendedor", extra={"reuniao_id": reuniao.id, "evento": evento})
        return None
    numero = numeros[0]
    resposta = evolution.get_client(numero.server).send_text(
        numero.instance_name, para_evolution(reuniao.vendedor.telefone), texto
    )
    mensagem_id = (resposta.get("key") or {}).get("id")
    if mensagem_id:
        from app.services.idempotency import register_event

        register_event(db, "envio-sistema", mensagem_id)
    logger.info(
        "vendedor avisado",
        extra={
            "reuniao_id": reuniao.id,
            "vendedor_id": reuniao.vendedor_id,
            "evento": evento,
            "numero_id": numero.id,
            "mensagem_id": mensagem_id,
        },
    )
    return mensagem_id


def avisar_vendedor(db: Session, reuniao: Reuniao) -> str | None:
    """Avisa o vendedor da reunião nova (uma vez só)."""
    if reuniao.vendedor_avisado_em is not None or not reuniao.vendedor.telefone:
        return None
    mensagem_id = _enviar_ao_vendedor(db, reuniao, mensagem_aviso(reuniao), "agendada")
    if mensagem_id is not None:
        reuniao.vendedor_avisado_em = datetime.now(timezone.utc)
    return mensagem_id


def avisar_cancelamento(db: Session, reuniao: Reuniao) -> str | None:
    """Avisa o vendedor que a reunião caiu. Só se ele chegou a ser avisado do agendamento."""
    if (
        reuniao.status != ReuniaoStatus.CANCELADA
        or reuniao.vendedor_avisado_em is None
        or reuniao.cancelamento_avisado_em is not None
        or not reuniao.vendedor.telefone
    ):
        return None
    mensagem_id = _enviar_ao_vendedor(db, reuniao, mensagem_cancelamento(reuniao), "cancelada")
    if mensagem_id is not None:
        reuniao.cancelamento_avisado_em = datetime.now(timezone.utc)
    return mensagem_id
