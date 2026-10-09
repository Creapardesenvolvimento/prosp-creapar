from datetime import datetime, timedelta, timezone

import pytest

from app.integrations.chatwoot import ChatwootError
from app.integrations.evolution import EvolutionError
from app.models import NumeroStatus, WhatsappNumber
from app.services import numeros as svc
from app.services.numeros import NumeroError


@pytest.fixture
def numero(db, server):
    numero, _ = svc.criar_numero(db, "Chip 1", server)
    db.commit()
    return numero


@pytest.fixture
def conectado(db, numero):
    svc.aplicar_estado(db, numero, "open")
    db.commit()
    return numero


def test_helpers():
    assert svc.telefone_do_jid("5511999990001@s.whatsapp.net") == "+5511999990001"
    assert svc.telefone_do_jid("5511999990001:12@s.whatsapp.net") == "+5511999990001"
    assert svc.telefone_do_jid(None) is None
    name = svc.gerar_instance_name("Chip São Paulo #1")
    assert name.startswith("creapar-chip-sao-paulo-1-")
    assert name != svc.gerar_instance_name("Chip São Paulo #1")


def test_criar_numero(db, server, evo):
    numero, qr = svc.criar_numero(db, "  Chip 1 ", server)

    assert qr == "data:image/png;base64,QR1"
    assert numero.status == NumeroStatus.AGUARDANDO_QR
    assert numero.nome_exibicao == "Chip 1"
    assert (numero.limite_diario_atual, numero.envios_hoje, numero.data_ativacao) == (0, 0, None)
    assert evo.called("create_instance") == [("create_instance", numero.instance_name)]
    # o webhook é configurado antes da leitura do QR, senão o "open" não chegaria
    assert evo.called("set_webhook") == [
        (
            "set_webhook",
            numero.instance_name,
            "https://painel.test/webhooks/evolution/test-webhook-token",
            ["CONNECTION_UPDATE", "MESSAGES_UPSERT"],
        )
    ]
    assert svc.numeros_no_rodizio(db) == []


def test_nome_repetido_e_recusado(db, server, numero):
    with pytest.raises(NumeroError):
        svc.criar_numero(db, "chip 1", server)


def test_falha_no_webhook_apaga_a_instancia_criada(db, server, evo):
    evo.fail["set_webhook"] = EvolutionError(500, "x")
    with pytest.raises(EvolutionError):
        svc.criar_numero(db, "Chip 1", server)
    assert len(evo.called("delete_instance")) == 1


def test_ao_conectar_entra_na_rampa_e_configura_chatwoot(db, numero, evo, cw):
    assert svc.aplicar_estado(db, numero, "open") is True

    assert numero.status == NumeroStatus.CONECTADO
    assert numero.telefone == "+5511999990001"
    assert numero.data_ativacao is not None
    assert numero.limite_diario_atual == 10  # dia 1 da rampa de teste
    assert svc.numeros_no_rodizio(db) == [numero]

    (_, instance, kwargs), = evo.called("set_chatwoot")
    assert instance == numero.instance_name
    assert kwargs["name_inbox"] == "Chip 1"
    assert numero.chatwoot_inbox_id == cw.inboxes["Chip 1"]


def test_conecta_mesmo_se_o_chatwoot_falhar_e_tenta_de_novo_depois(db, numero, evo, cw):
    cw.fail = ChatwootError("fora do ar")
    svc.aplicar_estado(db, numero, "open")
    db.commit()
    assert numero.status == NumeroStatus.CONECTADO
    assert numero.chatwoot_inbox_id is None

    cw.fail = None
    evo.state = "open"
    svc.conferir_conexoes(db)
    assert numero.chatwoot_inbox_id is not None


def test_queda_tira_do_rodizio_e_gera_alerta(db, conectado):
    assert svc.aplicar_estado(db, conectado, "close", 401) is True
    assert conectado.status == NumeroStatus.DESCONECTADO
    assert conectado.motivo_desconexao == 401
    assert svc.numeros_no_rodizio(db) == []
    assert svc.numeros_com_alerta(db) == [conectado]


def test_reconexao_do_mesmo_chip_mantem_a_rampa(db, conectado):
    conectado.data_ativacao = datetime.now(timezone.utc) - timedelta(days=5)
    svc.aplicar_estado(db, conectado, "close")
    svc.aplicar_estado(db, conectado, "open")
    assert conectado.status == NumeroStatus.CONECTADO
    assert conectado.limite_diario_atual == 20  # dia 6


def test_reconexao_com_outro_chip_reinicia_a_rampa(db, conectado, evo):
    conectado.data_ativacao = datetime.now(timezone.utc) - timedelta(days=30)
    conectado.envios_hoje = 15
    svc.aplicar_estado(db, conectado, "close")
    evo.owner_jid = "5511888880002@s.whatsapp.net"
    svc.aplicar_estado(db, conectado, "open")
    assert conectado.telefone == "+5511888880002"
    assert (conectado.limite_diario_atual, conectado.envios_hoje) == (10, 0)


def test_qr_expirado_nao_vira_desconectado(db, numero):
    assert svc.aplicar_estado(db, numero, "close") is False
    assert numero.status == NumeroStatus.AGUARDANDO_QR


def test_connecting_nao_derruba_numero_conectado(db, conectado):
    assert svc.aplicar_estado(db, conectado, "connecting") is False
    assert conectado.status == NumeroStatus.CONECTADO


def test_pausar_e_retomar(db, conectado, evo):
    svc.pausar(db, conectado)
    assert conectado.status == NumeroStatus.PAUSADO
    assert svc.numeros_no_rodizio(db) == []

    # evento "open" não tira o número da pausa
    svc.aplicar_estado(db, conectado, "open")
    assert conectado.status == NumeroStatus.PAUSADO

    evo.state = "open"
    svc.retomar(db, conectado)
    assert conectado.status == NumeroStatus.CONECTADO


def test_retomar_numero_que_caiu_durante_a_pausa(db, conectado, evo):
    svc.pausar(db, conectado)
    evo.state = "close"
    svc.retomar(db, conectado)
    assert conectado.status == NumeroStatus.DESCONECTADO


def test_pausar_exige_numero_conectado(db, numero):
    with pytest.raises(NumeroError):
        svc.pausar(db, numero)


def test_atualizar_qr(db, numero, evo):
    assert svc.atualizar_qr(db, numero) == "data:image/png;base64,QR2"
    evo.state = "open"
    assert svc.atualizar_qr(db, numero) is None
    assert numero.status == NumeroStatus.CONECTADO


def test_reconectar_volta_para_aguardando_qr(db, conectado):
    svc.aplicar_estado(db, conectado, "close")
    svc.reconectar(db, conectado)
    assert conectado.status == NumeroStatus.AGUARDANDO_QR


def test_banido_sai_do_rodizio_e_ignora_eventos(db, conectado):
    svc.marcar_banido(db, conectado)
    assert conectado.status == NumeroStatus.BANIDO
    assert svc.numeros_no_rodizio(db) == []
    assert svc.aplicar_estado(db, conectado, "open") is False
    assert conectado.status == NumeroStatus.BANIDO
    with pytest.raises(NumeroError):
        svc.marcar_banido(db, conectado)


def test_substituto_nao_herda_o_limite(db, server, conectado):
    conectado.data_ativacao = datetime.now(timezone.utc) - timedelta(days=30)
    conectado.limite_diario_atual = 40
    svc.marcar_banido(db, conectado)

    novo, _ = svc.criar_numero(db, "Chip 1 B", server, substitui=conectado)
    assert novo.substitui_numero_id == conectado.id
    assert novo.limite_diario_atual == 0

    svc.aplicar_estado(db, novo, "open")
    assert novo.limite_diario_atual == 10  # rampa do dia 1, não os 40 do antigo


def test_label_numero_perdido_nas_conversas_ativas(db, conectado, cw):
    inbox = conectado.chatwoot_inbox_id
    cw.conversations = [
        {"id": 1, "inbox_id": inbox, "status": "open", "labels": ["vip"]},
        {"id": 2, "inbox_id": inbox, "status": "resolved", "labels": []},
        {"id": 3, "inbox_id": inbox + 1, "status": "open", "labels": []},
    ]
    assert svc.marcar_conversas_numero_perdido(conectado) == 1
    assert "numero-perdido" in cw.labels
    assert [c["labels"] for c in cw.conversations] == [["vip", "numero-perdido"], [], []]


def test_remover_faz_logout_e_delete_e_mantem_o_historico(db, conectado, evo):
    numero_id = conectado.id
    svc.remover(db, conectado)
    db.commit()
    assert [c[0] for c in evo.calls[-2:]] == ["logout", "delete_instance"]
    assert db.get(WhatsappNumber, numero_id).status == NumeroStatus.REMOVIDO


def test_remover_tolera_instancia_que_ja_nao_existe(db, numero, evo):
    evo.fail["logout"] = EvolutionError(400, "sem sessão")
    evo.fail["delete_instance"] = EvolutionError(404, "not found")
    svc.remover(db, numero)
    assert numero.status == NumeroStatus.REMOVIDO


def test_remover_nao_marca_removido_se_a_evolution_falhar(db, numero, evo):
    evo.fail["delete_instance"] = EvolutionError(None, "sem rede")
    with pytest.raises(EvolutionError):
        svc.remover(db, numero)
    assert numero.status == NumeroStatus.AGUARDANDO_QR


def test_conferir_conexoes_detecta_queda(db, conectado, evo):
    evo.state = "close"
    assert svc.conferir_conexoes(db) == 1
    assert conectado.status == NumeroStatus.DESCONECTADO


def test_conferir_conexoes_instancia_apagada_conta_como_queda(db, conectado, evo):
    evo.fail["connection_state"] = EvolutionError(404, "not found")
    svc.conferir_conexoes(db)
    assert conectado.status == NumeroStatus.DESCONECTADO


def test_conferir_conexoes_ignora_falha_de_rede(db, conectado, evo):
    evo.fail["connection_state"] = EvolutionError(None, "timeout")
    assert svc.conferir_conexoes(db) == 0
    assert conectado.status == NumeroStatus.CONECTADO


def test_conferir_conexoes_nao_consulta_banidos_nem_removidos(db, conectado, evo):
    svc.marcar_banido(db, conectado)
    db.commit()
    evo.calls.clear()
    svc.conferir_conexoes(db)
    assert evo.called("connection_state") == []


def test_virada_do_dia(db, conectado):
    conectado.data_ativacao = datetime.now(timezone.utc) - timedelta(days=10)
    conectado.envios_hoje = 9
    svc.virada_do_dia(db)
    assert (conectado.envios_hoje, conectado.limite_diario_atual) == (0, 40)


def test_servidor_padrao_vem_do_env_uma_unica_vez(db):
    svc.garantir_servidor_padrao(db)
    svc.garantir_servidor_padrao(db)
    db.commit()
    from app.models import EvolutionServer
    from sqlalchemy import text

    (server,) = db.query(EvolutionServer).all()
    assert server.api_key == "evo-key"
    # no banco a chave fica criptografada
    assert db.execute(text("select api_key from evolution_servers")).scalar() != "evo-key"
