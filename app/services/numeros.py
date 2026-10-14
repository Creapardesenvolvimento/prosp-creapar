"""Ciclo de vida dos números de WhatsApp.

As funções alteram a sessão mas não fazem commit: quem chama (rota, webhook, task) decide.
"""

import logging
import re
import secrets
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations import chatwoot, evolution
from app.integrations.chatwoot import ChatwootError
from app.integrations.evolution import EvolutionError
from app.models import EvolutionServer, NumeroStatus, WhatsappNumber
from app.services import config_sistema
from app.services.rampa import limite_do_numero

logger = logging.getLogger(__name__)

WEBHOOK_EVENTS = ["CONNECTION_UPDATE", "MESSAGES_UPSERT", "SEND_MESSAGE"]
# Suba quando WEBHOOK_EVENTS mudar: os números já cadastrados recebem o webhook novo na próxima conferência.
WEBHOOK_VERSAO = 2
LABEL_NUMERO_PERDIDO = "numero-perdido"

# Status em que a instância ainda existe na Evolution e deve ser acompanhada.
MONITORADOS = (
    NumeroStatus.AGUARDANDO_QR,
    NumeroStatus.CONECTADO,
    NumeroStatus.PAUSADO,
    NumeroStatus.DESCONECTADO,
)


class NumeroError(Exception):
    pass


def webhook_url() -> str:
    settings = get_settings()
    return f"{settings.public_base_url.rstrip('/')}/webhooks/evolution/{settings.webhook_token}"


def gerar_instance_name(nome_exibicao: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", nome_exibicao).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:40] or "numero"
    return f"creapar-{slug}-{secrets.token_hex(3)}"


def telefone_do_jid(jid: str | None) -> str | None:
    """ "5511999998888:12@s.whatsapp.net" -> "+5511999998888" """
    digits = re.split(r"[:@]", jid or "")[0]
    return f"+{digits}" if digits.isdigit() else None


def garantir_servidor_padrao(db: Session) -> None:
    """Na primeira execução, cadastra o servidor Evolution que está no .env."""
    settings = get_settings()
    if not (settings.evolution_base_url and settings.evolution_api_key):
        return
    if db.scalar(select(func.count()).select_from(EvolutionServer)):
        return
    db.add(EvolutionServer(nome="Principal", base_url=settings.evolution_base_url, api_key=settings.evolution_api_key))
    db.flush()
    logger.info("servidor Evolution padrão cadastrado", extra={"base_url": settings.evolution_base_url})


def numeros_no_rodizio(db: Session) -> list[WhatsappNumber]:
    return list(db.scalars(select(WhatsappNumber).where(WhatsappNumber.status == NumeroStatus.CONECTADO)))


def numeros_com_alerta(db: Session) -> list[WhatsappNumber]:
    return list(db.scalars(select(WhatsappNumber).where(WhatsappNumber.status == NumeroStatus.DESCONECTADO)))


def numeros_pausados_automaticamente(db: Session) -> list[WhatsappNumber]:
    return list(
        db.scalars(
            select(WhatsappNumber).where(
                WhatsappNumber.status == NumeroStatus.PAUSADO,
                WhatsappNumber.motivo_pausa.is_not(None),
                WhatsappNumber.motivo_pausa != "manual",
            )
        )
    )


def criar_numero(
    db: Session, nome_exibicao: str, server: EvolutionServer, substitui: WhatsappNumber | None = None
) -> tuple[WhatsappNumber, str | None]:
    """Cria a instância na Evolution e devolve (número, QR em base64)."""
    nome = nome_exibicao.strip()
    if not nome:
        raise NumeroError("Informe um nome de exibição.")
    if not server.ativo:
        raise NumeroError("Este servidor Evolution está inativo.")
    # O nome vira o nome da caixa de entrada no Chatwoot, então não pode repetir.
    if db.scalar(select(WhatsappNumber).where(func.lower(WhatsappNumber.nome_exibicao) == nome.lower())):
        raise NumeroError("Já existe um número com esse nome. Use um nome diferente (ex.: com um sufixo).")

    instance_name = gerar_instance_name(nome)
    client = evolution.get_client(server)
    created = client.create_instance(instance_name)
    try:
        # O webhook precisa existir antes do QR ser lido, senão o "open" não chega até nós.
        client.set_webhook(instance_name, webhook_url(), WEBHOOK_EVENTS)
    except EvolutionError:
        try:
            client.delete_instance(instance_name)
        except EvolutionError:
            logger.warning("instância órfã na Evolution", extra={"instance": instance_name})
        raise

    numero = WhatsappNumber(
        server=server,
        nome_exibicao=nome,
        instance_name=instance_name,
        status=NumeroStatus.AGUARDANDO_QR,
        webhook_versao=WEBHOOK_VERSAO,
        substitui_numero_id=substitui.id if substitui else None,
    )
    db.add(numero)
    db.flush()
    logger.info(
        "número criado",
        extra={"numero_id": numero.id, "instance": instance_name, "substitui": numero.substitui_numero_id},
    )
    return numero, (created.get("qrcode") or {}).get("base64")


def configurar_chatwoot(db: Session, numero: WhatsappNumber) -> bool:
    """Liga a instância ao Chatwoot e guarda o id da caixa de entrada. Pode ser repetida."""
    settings = get_settings()
    if not (settings.chatwoot_base_url and settings.chatwoot_token and settings.chatwoot_account_id):
        logger.warning("Chatwoot não configurado no .env", extra={"numero_id": numero.id})
        return False
    try:
        cw = chatwoot.get_client()
        inbox_id = cw.find_inbox_id(numero.nome_exibicao)
        if inbox_id is None:
            evolution.get_client(numero.server).set_chatwoot(
                numero.instance_name,
                url=settings.chatwoot_base_url,
                account_id=settings.chatwoot_account_id,
                token=settings.chatwoot_token,
                name_inbox=numero.nome_exibicao,
            )
            inbox_id = cw.find_inbox_id(numero.nome_exibicao)
    except (EvolutionError, ChatwootError) as exc:
        logger.error("falha ao configurar Chatwoot", extra={"numero_id": numero.id, "erro": str(exc)})
        return False
    if inbox_id is None:
        logger.warning("caixa de entrada ainda não apareceu no Chatwoot", extra={"numero_id": numero.id})
        return False
    numero.chatwoot_inbox_id = inbox_id
    logger.info("Chatwoot configurado", extra={"numero_id": numero.id, "inbox_id": inbox_id})
    return True


def _ao_conectar(db: Session, numero: WhatsappNumber) -> None:
    telefone = None
    try:
        telefone = telefone_do_jid(evolution.get_client(numero.server).fetch_instance(numero.instance_name).get("ownerJid"))
    except (EvolutionError, IndexError) as exc:
        logger.warning("não consegui ler o telefone da instância", extra={"numero_id": numero.id, "erro": str(exc)})

    chip_trocado = bool(telefone and numero.telefone and telefone != numero.telefone)
    if telefone:
        numero.telefone = telefone
    if numero.data_ativacao is None or chip_trocado:
        # Chip novo (ou outro chip lido na reconexão): a rampa começa do dia 1.
        numero.data_ativacao = datetime.now(timezone.utc)
        numero.envios_hoje = 0
    numero.limite_diario_atual = limite_do_numero(numero.data_ativacao, rampa=config_sistema.rampa(db))
    numero.status = NumeroStatus.CONECTADO
    numero.motivo_desconexao = None
    logger.info(
        "número conectado",
        extra={"numero_id": numero.id, "telefone": numero.telefone, "limite": numero.limite_diario_atual},
    )
    if numero.chatwoot_inbox_id is None:
        configurar_chatwoot(db, numero)


def aplicar_estado(db: Session, numero: WhatsappNumber, state: str | None, reason: int | None = None) -> bool:
    """Aplica o estado de conexão informado pela Evolution. Retorna True se o status mudou."""
    if numero.status not in MONITORADOS:
        return False
    if state == "open":
        if numero.status in (NumeroStatus.AGUARDANDO_QR, NumeroStatus.DESCONECTADO):
            _ao_conectar(db, numero)
            return True
    elif state == "close":
        # Em aguardando_qr o "close" é só o QR que expirou; o painel pede outro.
        if numero.status in (NumeroStatus.CONECTADO, NumeroStatus.PAUSADO):
            numero.status = NumeroStatus.DESCONECTADO
            numero.motivo_desconexao = reason
            logger.warning(
                "número desconectado, fora do rodízio", extra={"numero_id": numero.id, "motivo": reason}
            )
            return True
    return False


def atualizar_qr(db: Session, numero: WhatsappNumber) -> str | None:
    """Usado pelo painel enquanto espera a leitura: confere a conexão e devolve um QR atual."""
    client = evolution.get_client(numero.server)
    aplicar_estado(db, numero, client.connection_state(numero.instance_name))
    if numero.status != NumeroStatus.AGUARDANDO_QR:
        return None
    return client.connect(numero.instance_name).get("base64")


def pausar(db: Session, numero: WhatsappNumber) -> None:
    if numero.status != NumeroStatus.CONECTADO:
        raise NumeroError("Só é possível pausar um número conectado.")
    numero.status = NumeroStatus.PAUSADO
    numero.motivo_pausa = "manual"
    logger.info("número pausado", extra={"numero_id": numero.id})


def retomar(db: Session, numero: WhatsappNumber) -> None:
    if numero.status != NumeroStatus.PAUSADO:
        raise NumeroError("Este número não está pausado.")
    state = evolution.get_client(numero.server).connection_state(numero.instance_name)
    numero.status = NumeroStatus.CONECTADO if state == "open" else NumeroStatus.DESCONECTADO
    numero.motivo_pausa = None
    logger.info("número retomado", extra={"numero_id": numero.id, "status": numero.status})


def reconectar(db: Session, numero: WhatsappNumber) -> None:
    """Volta para aguardando_qr; o painel passa a exibir um QR novo."""
    if numero.status not in (NumeroStatus.DESCONECTADO, NumeroStatus.AGUARDANDO_QR):
        raise NumeroError("Só é possível reconectar um número desconectado.")
    numero.status = NumeroStatus.AGUARDANDO_QR
    logger.info("reconexão solicitada", extra={"numero_id": numero.id})


def liberar_envios_pendentes(db: Session, numero: WhatsappNumber) -> int:
    """Devolve à fila geral os envios pendentes do número.

    A fila de envios é criada na Fase 4; até lá não há o que devolver.
    """
    return 0


def marcar_banido(db: Session, numero: WhatsappNumber) -> None:
    """Tira o número do rodízio de vez. Depois do commit, chamar a task que põe a label no Chatwoot."""
    if numero.status in (NumeroStatus.BANIDO, NumeroStatus.REMOVIDO):
        raise NumeroError("Este número já foi marcado como banido ou removido.")
    numero.status = NumeroStatus.BANIDO
    devolvidos = liberar_envios_pendentes(db, numero)
    logger.warning("número marcado como banido/perdido", extra={"numero_id": numero.id, "envios_devolvidos": devolvidos})


def marcar_conversas_numero_perdido(numero: WhatsappNumber) -> int:
    if numero.chatwoot_inbox_id is None:
        return 0
    cw = chatwoot.get_client()
    cw.ensure_label(LABEL_NUMERO_PERDIDO)
    total = sum(cw.add_label(c, LABEL_NUMERO_PERDIDO) for c in cw.active_conversations(numero.chatwoot_inbox_id))
    logger.info("conversas marcadas como numero-perdido", extra={"numero_id": numero.id, "total": total})
    return total


def remover(db: Session, numero: WhatsappNumber) -> None:
    """Logout + delete na Evolution. O registro fica no banco como histórico."""
    if numero.status == NumeroStatus.REMOVIDO:
        raise NumeroError("Este número já foi removido.")
    client = evolution.get_client(numero.server)
    try:
        client.logout(numero.instance_name)
    except EvolutionError:
        pass  # instância que nunca conectou ou já caiu não tem sessão para encerrar
    try:
        client.delete_instance(numero.instance_name)
    except EvolutionError as exc:
        if exc.status != 404:
            raise
    numero.status = NumeroStatus.REMOVIDO
    logger.info("número removido", extra={"numero_id": numero.id, "instance": numero.instance_name})


def conferir_conexoes(db: Session) -> int:
    """Confere o connectionState de todas as instâncias acompanhadas. Retorna quantas mudaram."""
    mudancas = 0
    numeros = db.scalars(select(WhatsappNumber).where(WhatsappNumber.status.in_(MONITORADOS))).all()
    for numero in numeros:
        try:
            state = evolution.get_client(numero.server).connection_state(numero.instance_name)
        except EvolutionError as exc:
            if exc.status != 404:
                logger.error("falha ao conferir conexão", extra={"numero_id": numero.id, "erro": str(exc)})
                continue
            state = "close"  # a instância não existe mais na Evolution
        mudancas += aplicar_estado(db, numero, state)
        if numero.status in (NumeroStatus.CONECTADO, NumeroStatus.PAUSADO) and numero.chatwoot_inbox_id is None:
            configurar_chatwoot(db, numero)
        if (numero.webhook_versao or 0) < WEBHOOK_VERSAO:
            try:
                evolution.get_client(numero.server).set_webhook(numero.instance_name, webhook_url(), WEBHOOK_EVENTS)
                numero.webhook_versao = WEBHOOK_VERSAO
                logger.info("webhook do número atualizado", extra={"numero_id": numero.id})
            except EvolutionError as exc:
                logger.error("falha ao atualizar webhook", extra={"numero_id": numero.id, "erro": str(exc)})
        db.commit()
    return mudancas


def virada_do_dia(db: Session) -> None:
    """Zera os envios do dia e recalcula o limite de cada número pela rampa."""
    agora = datetime.now(timezone.utc)
    rampa = config_sistema.rampa(db)
    for numero in db.scalars(select(WhatsappNumber).where(WhatsappNumber.status.in_(MONITORADOS))):
        numero.envios_hoje = 0
        numero.limite_diario_atual = limite_do_numero(numero.data_ativacao, agora, rampa)
    logger.info("virada do dia: limites recalculados")
