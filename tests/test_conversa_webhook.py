from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from agente_fakes import FakeIA, ferramenta, montar_docx, resposta
from app.db import SessionLocal
from app.models import Autor, Direcao, Envio, EnvioStatus, EnvioTipo, Lead, LeadStatus, Mensagem
from app.services import agente, config_sistema, ia, roteiros
from app.tasks.agente import responder_lead

WEBHOOK = "/webhooks/evolution/test-webhook-token"
TEL = "5524999990001"


@pytest.fixture
def fake_ia(monkeypatch):
    fake = FakeIA()
    monkeypatch.setattr(ia, "get_client", lambda: fake)
    return fake


@pytest.fixture
def lead(db, conectado):
    roteiros.importar(db, roteiros.ler_docx(montar_docx()))
    lead = Lead(nome="Pousada Sol", telefone=f"+{TEL}", whatsapp_jid=f"{TEL}@s.whatsapp.net", categoria="Pousada",
                status=LeadStatus.ENVIADO, numero_id=conectado.id, segmento="Pousadas", porte="pequeno")
    db.add(lead)
    db.flush()
    db.add(Mensagem(lead_id=lead.id, numero_id=conectado.id, direcao=Direcao.SAIDA, autor=Autor.SISTEMA, texto="Olá, bom dia!"))
    db.commit()
    return lead


def recebida(texto="Bom dia", msg_id="IN1", tipo="conversation", **extra):
    message = {"conversation": texto} if tipo == "conversation" else {tipo: extra.pop("conteudo", {})}
    return {"event": "messages.upsert", "instance": "chip",
            "data": {"key": {"remoteJid": f"{TEL}@s.whatsapp.net", "fromMe": False, "id": msg_id}, "messageType": tipo, "message": message, **extra}}


def propria(texto, msg_id, evento="messages.upsert", telefone=TEL):
    return {"event": evento, "instance": "chip",
            "data": {"key": {"remoteJid": f"{telefone}@s.whatsapp.net", "fromMe": True, "id": msg_id}, "messageType": "conversation", "message": {"conversation": texto}}}


def ler(lead_id):
    with SessionLocal() as s:
        return s.get(Lead, lead_id)


def depois(segundos=60):
    return (datetime.now(timezone.utc) + timedelta(seconds=segundos)).isoformat()


# --- resposta agendada e agrupamento ---------------------------------------------------


def test_resposta_do_lead_agenda_a_ia_e_ela_responde(client, db, lead, fake_ia, evo):
    assert client.post(WEBHOOK, json=recebida("Bom dia, quem fala?")).json() == {"status": "ok"}
    atual = ler(lead.id)
    assert atual.status == LeadStatus.RESPONDEU and atual.responder_em is not None
    assert fake_ia.chamadas == []  # ainda esperando o lead terminar de escrever

    fake_ia.fila.append(resposta(ferramenta("responder", {"mensagem": "Oi! Aqui é a Bruna, da Brafort10.", "etapa": "atencao"})))
    assert responder_lead(lead.id, agora=depois()) == "respondeu"
    atual = ler(lead.id)
    assert (atual.status, atual.etapa, atual.responder_em) == (LeadStatus.EM_CONVERSA, "interesse", None)
    assert evo.called("send_text")[-1][3] == "Aqui é a Bruna, da Brafort10."


def test_mensagens_seguidas_viram_uma_resposta_so(client, db, lead, fake_ia, evo):
    client.post(WEBHOOK, json=recebida("Bom dia", msg_id="IN1"))
    primeira = ler(lead.id).responder_em
    client.post(WEBHOOK, json=recebida("quem fala?", msg_id="IN2"))
    segunda = ler(lead.id).responder_em
    assert segunda >= primeira  # a segunda mensagem empurra a resposta para frente

    # a tarefa agendada pela primeira mensagem acorda antes do novo prazo e não responde
    assert responder_lead(lead.id, agora=(primeira.replace(tzinfo=timezone.utc) - timedelta(seconds=1)).isoformat()) == "aguardando"

    fake_ia.fila.append(resposta(ferramenta("responder", {"mensagem": "Oi! Sou a Bruna.", "etapa": "atencao"})))
    assert responder_lead(lead.id, agora=depois()) == "respondeu"
    assert fake_ia.chamadas[0]["messages"] == [{"role": "user", "content": "Bom dia\nquem fala?"}]
    assert responder_lead(lead.id, agora=depois()) == "aguardando"  # só uma resposta
    assert len(evo.called("send_text")) == 1


def test_espera_vem_das_configuracoes(client, db, lead):
    config_sistema.salvar(db, {"ia_espera_segundos": "45"})
    db.commit()
    antes = datetime.now(timezone.utc)
    client.post(WEBHOOK, json=recebida())
    espera = (ler(lead.id).responder_em.replace(tzinfo=timezone.utc) - antes).total_seconds()
    assert 44 <= espera <= 47


def test_reenvio_da_mesma_mensagem_nao_duplica(client, db, lead):
    client.post(WEBHOOK, json=recebida(msg_id="IN1"))
    reenvio = recebida(msg_id="IN1")
    reenvio["date_time"] = "outro corpo"
    client.post(WEBHOOK, json=reenvio)
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Mensagem).where(Mensagem.direcao == Direcao.ENTRADA)) == 1


def test_reacao_e_ignorada(client, db, lead):
    client.post(WEBHOOK, json=recebida(tipo="reactionMessage", conteudo={"text": "👍"}))
    assert ler(lead.id).status == LeadStatus.ENVIADO


# --- passagem para humano ------------------------------------------------------------------


def test_audio_passa_para_humano_e_marca_no_chatwoot(client, db, lead, cw, conectado):
    cw.conversations = [{"id": 9, "inbox_id": conectado.chatwoot_inbox_id, "telefone": f"+{TEL}", "status": "open", "labels": []}]
    client.post(WEBHOOK, json=recebida(tipo="audioMessage", conteudo={"seconds": 12}))
    atual = ler(lead.id)
    assert (atual.ia_ativa, atual.status) == (False, LeadStatus.HUMANO_ASSUMIU)
    assert "audio" in atual.handoff_motivo
    assert cw.conversations[0]["labels"] == ["humano"] and "humano" in cw.labels
    with SessionLocal() as s:
        assert s.scalar(select(Mensagem.tipo).where(Mensagem.direcao == Direcao.ENTRADA)) == "audio"


def test_ia_desligada_nao_responde_mas_guarda_historico(client, db, lead, fake_ia):
    agente.passar_para_humano(db, lead, "teste")
    db.commit()
    client.post(WEBHOOK, json=recebida("ainda está aí?"))
    assert ler(lead.id).responder_em is None
    with SessionLocal() as s:
        assert s.scalar(select(Mensagem.texto).where(Mensagem.direcao == Direcao.ENTRADA)) == "ainda está aí?"


def test_opt_out_desliga_a_ia(client, db, lead):
    client.post(WEBHOOK, json=recebida("SAIR"))
    atual = ler(lead.id)
    assert (atual.status, atual.ia_ativa, atual.responder_em) == (LeadStatus.OPT_OUT, False, None)


# --- REGRA CRÍTICA: humano respondeu ----------------------------------------------------------


def test_mensagem_do_sistema_nao_conta_como_humano(client, db, lead):
    agente.marcar_como_do_sistema(db, "OUT1")
    db.commit()
    client.post(WEBHOOK, json=propria("Olá, bom dia!", "OUT1"))
    assert ler(lead.id).ia_ativa is True


def test_humano_respondeu_pelo_celular_ou_chatwoot(client, db, lead, cw, conectado):
    cw.conversations = [{"id": 9, "inbox_id": conectado.chatwoot_inbox_id, "telefone": f"+{TEL}", "status": "open", "labels": []}]
    client.post(WEBHOOK, json=propria("Oi, aqui é o Carlos da Brafort10", "HUMANO1"))
    atual = ler(lead.id)
    assert (atual.ia_ativa, atual.status) == (False, LeadStatus.HUMANO_ASSUMIU)
    assert "humano respondeu" in atual.handoff_motivo
    assert cw.conversations[0]["labels"] == ["humano"]
    with SessionLocal() as s:
        msg = s.scalar(select(Mensagem).where(Mensagem.autor == Autor.HUMANO))
        assert msg.texto == "Oi, aqui é o Carlos da Brafort10"


def test_humano_pelo_evento_send_message(client, db, lead):
    client.post(WEBHOOK, json=propria("Olá, sou do time", "HUMANO2", evento="SEND_MESSAGE"))
    assert ler(lead.id).status == LeadStatus.HUMANO_ASSUMIU


def test_webhook_do_proprio_envio_antes_do_id_ser_gravado(client, db, lead):
    # o sistema gravou o envio (com o texto) mas a resposta da Evolution ainda não trouxe o id
    db.add(Envio(lead_id=lead.id, numero_id=lead.numero_id, tipo=EnvioTipo.IA, texto="Oi! Aqui é a Bruna.", status=EnvioStatus.PENDENTE))
    db.commit()
    client.post(WEBHOOK, json=propria("Oi! Aqui é a Bruna.", "OUT-SEM-ID"))
    assert ler(lead.id).ia_ativa is True
    with SessionLocal() as s:
        assert agente.eh_do_sistema(s, "OUT-SEM-ID")


def test_mensagem_propria_para_quem_nao_e_lead_e_ignorada(client, db, lead):
    client.post(WEBHOOK, json=propria("Nova reunião agendada", "AVISO1", telefone="5511977776666"))
    assert ler(lead.id).ia_ativa is True


def test_humano_assumiu_e_a_ia_para_mesmo_com_resposta_agendada(client, db, lead, fake_ia):
    client.post(WEBHOOK, json=recebida("Bom dia"))
    client.post(WEBHOOK, json=propria("Deixa comigo, já te respondo", "HUMANO3"))
    assert responder_lead(lead.id, agora=depois()) in ("aguardando", "nada")
    assert fake_ia.chamadas == []


# --- telas ----------------------------------------------------------------------------------------


def test_telas_de_roteiros_e_leads(logged_client, db, lead):
    resp = logged_client.post("/roteiros/importar", files={"arquivo": ("brafort.docx", montar_docx(), "application/octet-stream")})
    assert "ok=" in resp.headers["location"]
    pagina = logged_client.get("/roteiros")
    assert "Pousadas" in pagina.text and "Lavanderias" in pagina.text

    with SessionLocal() as s:
        from app.models import Segmento

        seg_id = s.scalar(select(Segmento.id).where(Segmento.nome == "Pousadas"))
    assert "Treinamento certificado para a equipe." in logged_client.get(f"/roteiros/{seg_id}").text
    logged_client.post(f"/roteiros/{seg_id}", data={"palavras_chave": "pousada, hotel", "foco_pequeno": "Economia", "texto_pequeno_acao": "Bora conversar?"})
    assert "Bora conversar?" in logged_client.get(f"/roteiros/{seg_id}").text

    resp = logged_client.post("/roteiros/importar", files={"arquivo": ("x.docx", b"lixo", "application/octet-stream")})
    assert "erro=" in resp.headers["location"]

    detalhe = logged_client.get(f"/leads/{lead.id}")
    assert "Pousada Sol" in detalhe.text and "Olá, bom dia!" in detalhe.text and "Pausar IA" in detalhe.text
    logged_client.post(f"/leads/{lead.id}/pausar-ia")
    assert ler(lead.id).ia_ativa is False
    logged_client.post(f"/leads/{lead.id}/devolver-ia")
    assert ler(lead.id).ia_ativa is True
    assert "Pousada Sol" in logged_client.get("/leads?filtro=conversa").text  # pausar e devolver deixam o lead em conversa


def test_configuracoes_da_empresa(logged_client, db):
    pagina = logged_client.get("/config")
    assert 'value="Bruna"' in pagina.text and 'value="Brafort10"' in pagina.text and 'value="até 50%"' in pagina.text
    logged_client.post("/config", data={"atendente_nome": "Bruna Lima", "disparo_dias": ["0", "1", "2", "3", "4"]})
    assert config_sistema.valores(db)["atendente_nome"] == "Bruna Lima"
