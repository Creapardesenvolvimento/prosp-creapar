from datetime import timedelta

import pytest

from app.services import agenda, config_sistema, numeros
from app.services.config_sistema import ConfigError


def test_padroes_de_fabrica(db):
    v = config_sistema.valores(db)
    assert v["disparo_dias"] == [0, 1, 2, 3, 4]
    assert (v["disparo_inicio"], v["disparo_fim"]) == ("09:00", "18:00")
    assert (v["intervalo_min_minutos"], v["intervalo_max_minutos"]) == (2.0, 6.0)
    assert (v["pausa_taxa_minima"], v["pausa_janela_envios"]) == (5.0, 50)
    assert v["rampa"] == "1:10,4:20,8:40"  # do .env de teste


def test_salvar_e_restaurar(db):
    config_sistema.salvar(db, {"intervalo_min_minutos": "3,5", "intervalo_max_minutos": "10", "disparo_dias": ["0", "2"]})
    db.commit()
    regras = config_sistema.regras_disparo(db)
    assert (regras.intervalo_min_seg, regras.intervalo_max_seg, regras.dias) == (210, 600, frozenset({0, 2}))

    config_sistema.restaurar_padroes(db)
    db.commit()
    assert config_sistema.valores(db)["intervalo_min_minutos"] == 2.0


@pytest.mark.parametrize(
    "dados",
    [
        {"intervalo_min_minutos": "10", "intervalo_max_minutos": "5"},
        {"disparo_inicio": "18:00", "disparo_fim": "09:00"},
        {"disparo_inicio": "25:00"},
        {"disparo_dias": []},
        {"rampa": "2:10"},
        {"rampa": "abc"},
        {"pausa_taxa_minima": "150"},
        {"pausa_janela_envios": "x"},
    ],
)
def test_valores_invalidos_sao_recusados(db, dados):
    with pytest.raises(ConfigError):
        config_sistema.salvar(db, dados)


def test_rampa_do_painel_vale_para_os_numeros(db, server):
    config_sistema.salvar(db, {"rampa": "1:3,2:6"})
    numero, _ = numeros.criar_numero(db, "Chip", server)
    numeros.aplicar_estado(db, numero, "open")
    assert numero.limite_diario_atual == 3


def test_regras_da_agenda_do_painel(db):
    config_sistema.salvar(db, {"reuniao_minutos": "45", "reuniao_buffer_minutos": "0"})
    regras = agenda.regras_do_sistema(db)
    assert (regras.duracao, regras.buffer) == (timedelta(minutes=45), timedelta(0))


def test_tela_de_configuracoes(logged_client):
    pagina = logged_client.get("/config")
    assert "Intervalo mínimo entre envios" in pagina.text and "Padrão de fábrica: 2." in pagina.text

    resp = logged_client.post("/config", data={"intervalo_min_minutos": "3", "intervalo_max_minutos": "8", "disparo_dias": ["0", "1"]})
    assert "ok=" in resp.headers["location"]
    assert 'name="intervalo_min_minutos" value="3"' in logged_client.get("/config").text

    resp = logged_client.post("/config", data={"intervalo_min_minutos": "9", "intervalo_max_minutos": "8", "disparo_dias": ["0"]})
    assert "erro=" in resp.headers["location"]

    logged_client.post("/config", data={"restaurar": "1"})
    assert 'name="intervalo_min_minutos" value="2"' in logged_client.get("/config").text


def test_checkbox_do_pitch(logged_client):
    from app.db import SessionLocal

    def valor():
        with SessionLocal() as s:  # sessão curta: uma leitura aberta travaria o SQLite para a próxima requisição
            return config_sistema.valores(s)["pitch_em_sequencia"]

    assert valor() is True
    logged_client.post("/config", data={"disparo_dias": ["0"]})  # checkbox desmarcado não vem no formulário
    assert valor() is False
    logged_client.post("/config", data={"disparo_dias": ["0"], "pitch_em_sequencia": "1"})
    assert valor() is True
    assert "Mandar Interesse, Desejo e Ação em sequência" in logged_client.get("/config").text
