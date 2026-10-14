import pytest
from sqlalchemy import func, select

from app.models import Blacklist, Lead, LeadStatus
from app.services import mensagens
from app.services.mensagens import eh_opt_out

WEBHOOK = "/webhooks/evolution/test-webhook-token"


@pytest.mark.parametrize(
    "texto",
    [
        "SAIR",
        "sair",
        "Sair.",
        "quero sair",
        "SAIR por favor",
        "Pare",
        "stop",
        "Descadastrar",
        "não quero receber mais mensagens",
        "Nao quero mais receber isso",
        "para de me mandar mensagem",
        "parem de mandar isso",
        "não me mande mais nada",
        "me tira da sua lista",
        "tire meu número",
        "não entre mais em contato",
        "Não tenho interesse, não insista",
        "vou denunciar esse número",
        "isso é spam",
    ],
)
def test_reconhece_pedido_de_opt_out(texto):
    assert eh_opt_out(texto)


@pytest.mark.parametrize(
    "texto",
    [
        None,
        "",
        "oi, tudo bem?",
        "para quem é?",
        "pode mandar o orçamento",
        "quero saber mais",
        "agora não posso, me chama amanhã",
        "vou sair para almoçar e te respondo",
        "não tenho interesse no momento",  # recusa educada não é pedido de bloqueio (vira sem_interesse na Fase 5)
        "qual o preço?",
        "pare tudo, que proposta incrível",
    ],
)
def test_nao_confunde_conversa_normal_com_opt_out(texto):
    assert not eh_opt_out(texto)


def payload(texto, telefone="5519911110001", from_me=False, msg_id="MSG1", **key_extra):
    return {
        "event": "messages.upsert",
        "instance": "chip",
        "data": {
            "key": {"remoteJid": f"{telefone}@s.whatsapp.net", "fromMe": from_me, "id": msg_id, **key_extra},
            "message": {"conversation": texto},
        },
    }


@pytest.fixture
def lead(db):
    lead = Lead(nome="Padaria", telefone="+5519911110001", whatsapp_jid="5519911110001@s.whatsapp.net", status=LeadStatus.ENVIADO)
    db.add(lead)
    db.commit()
    return lead


def test_resposta_marca_que_respondeu(db, lead):
    assert mensagens.registrar_recebida(db, payload("oi, quem fala?")) == lead
    assert lead.status == LeadStatus.RESPONDEU and lead.respondeu_em is not None


def test_opt_out_vai_para_a_blacklist_permanente(db, lead):
    mensagens.registrar_recebida(db, payload("SAIR"))
    db.commit()
    assert lead.status == LeadStatus.OPT_OUT
    assert db.scalar(select(Blacklist.telefone)) == "+5519911110001"
    # repetir não duplica
    mensagens.registrar_recebida(db, payload("SAIR", msg_id="MSG2"))
    db.commit()
    assert db.scalar(select(func.count()).select_from(Blacklist)) == 1


def test_mensagem_nossa_e_ignorada(db, lead):
    assert mensagens.registrar_recebida(db, payload("SAIR", from_me=True)) is None
    assert lead.status == LeadStatus.ENVIADO


def test_contato_que_nao_e_lead_e_ignorado(db, lead):
    assert mensagens.registrar_recebida(db, payload("SAIR", telefone="5511999990000")) is None
    assert db.scalar(select(func.count()).select_from(Blacklist)) == 0


def test_grupo_e_ignorado(db, lead):
    data = payload("SAIR")
    data["data"]["key"]["remoteJid"] = "120363000000000000@g.us"
    assert mensagens.registrar_recebida(db, data) is None


def test_lid_com_numero_alternativo(db, lead):
    data = payload("sair")
    data["data"]["key"]["remoteJid"] = "123456789012345@lid"
    data["data"]["key"]["remoteJidAlt"] = "5519911110001@s.whatsapp.net"
    assert mensagens.registrar_recebida(db, data) == lead
    assert lead.status == LeadStatus.OPT_OUT


def test_texto_de_mensagem_estendida():
    assert mensagens.extrair_texto({"message": {"extendedTextMessage": {"text": "SAIR"}}}) == "SAIR"
    assert mensagens.extrair_texto({"message": {"imageMessage": {"caption": "pare"}}}) == "pare"
    assert mensagens.extrair_texto({"message": {"audioMessage": {}}}) is None


def test_webhook_messages_upsert(client, db, lead):
    assert client.post(WEBHOOK, json=payload("me tira da lista")).json() == {"status": "ok"}
    db.refresh(lead)
    assert lead.status == LeadStatus.OPT_OUT
    db.commit()  # fecha a leitura antes da próxima requisição (o SQLite trava a escrita)
    # a Evolution reenvia a mesma mensagem com outro corpo: ignorada pelo id
    reenvio = payload("me tira da lista")
    reenvio["date_time"] = "2026-10-14T12:00:00Z"
    assert client.post(WEBHOOK, json=reenvio).json() == {"status": "ok"}
    assert db.scalar(select(func.count()).select_from(Blacklist)) == 1
