"""Vários clientes: nada de um aparece, é enviado ou é apagado no outro."""

import random

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import (
    Blacklist,
    Busca,
    BuscaStatus,
    Campanha,
    CampanhaStatus,
    Cliente,
    Empresa,
    Envio,
    EnvioStatus,
    Lead,
    LeadStatus,
    Mensagem,
    Segmento,
    Vendedor,
    WhatsappNumber,
)
from app.services import clientes, config_sistema, numeros, roteiros
from app.tenancy import TODOS, no_cliente, usar_cliente
from tests.conftest import CLIENTE_PADRAO, cadastrar_instancia
from tests.test_disparo import SEGUNDA_9H, criar_campanha, criar_numero
from tests.test_opt_out import payload


@pytest.fixture(autouse=True)
def sem_virada(monkeypatch):
    monkeypatch.setattr(numeros, "garantir_virada", lambda db, agora=None: False)


@pytest.fixture
def outro(db):
    """Segundo cliente, com nomes que não aparecem no primeiro."""
    cliente = clientes.criar(db, nome="Clínica Sorriso", atendente_nome="Carla", economia_texto="até 30%")
    db.commit()
    return cliente


def montar(db, server, prefixo: str, telefone_lead: str):
    """Número, campanha ativa, lead na fila, empresa, vendedor e segmento do cliente da sessão."""
    n = criar_numero(db, server, f"{prefixo} Chip", 10)
    c = criar_campanha(db, [n])
    c.nome = f"{prefixo} Campanha"
    busca = Busca(termo=f"{prefixo} busca", cidade="Campinas", limite=1, status=BuscaStatus.CONCLUIDA)
    db.add(busca)
    db.flush()
    lead = Lead(nome=f"{prefixo} Padaria", telefone=telefone_lead, status=LeadStatus.NA_FILA, campanha_id=c.id, busca_id=busca.id,
                whatsapp_jid=f"{telefone_lead[1:]}@s.whatsapp.net")
    db.add(lead)
    db.flush()
    db.add(Empresa(busca_id=busca.id, nome=f"{prefixo} Padaria", telefone=telefone_lead, celular=True, lead_id=lead.id, dados={}))
    db.add(Vendedor(nome=f"{prefixo} Vendedor", ativo=True))
    db.add(Segmento(nome=f"{prefixo} Segmento", palavras_chave=""))
    db.commit()
    return n, c, lead


def test_cada_tela_mostra_so_o_cliente_escolhido(logged_client, db, server, outro):
    montar(db, server, "Alfa", "+5519955550001")
    with no_cliente(db, outro.id):
        montar(db, server, "Beta", "+5519955550001")  # mesma empresa nos dois clientes: permitido

    telas = ["/", "/numeros", "/campanhas", "/buscas", "/empresas", "/leads?filtro=todos", "/prospeccao", "/agenda", "/vendedores", "/roteiros"]
    for tela in telas:
        texto = logged_client.get(tela).text
        assert "Beta" not in texto, tela
    assert "Alfa Padaria" in logged_client.get("/empresas").text

    resp = logged_client.post("/cliente/trocar", data={"cliente_id": str(outro.id)}, headers={"referer": "https://painel.test/empresas?situacao=x"})
    assert resp.headers["location"] == "/empresas"
    for tela in telas:
        texto = logged_client.get(tela).text
        assert "Alfa" not in texto, tela
    assert "Beta Padaria" in logged_client.get("/empresas").text
    assert 'selected>Clínica Sorriso' in logged_client.get("/").text

    # o id de um registro do outro cliente não abre
    with SessionLocal() as s:
        campanha_alfa = s.scalar(select(Campanha.id).where(Campanha.nome == "Alfa Campanha").execution_options(**TODOS))
    assert logged_client.get(f"/campanhas/{campanha_alfa}").status_code == 404


def test_configuracoes_e_blacklist_por_cliente(db, outro):
    config_sistema.salvar(db, {"meta_diaria": "500", "notificar_telefone": "(11) 98888-7777"})
    db.add(Blacklist(telefone="+5519955550001", motivo="opt-out"))
    db.commit()
    with no_cliente(db, outro.id):
        v = config_sistema.valores(db)
        assert (v["meta_diaria"], v["notificar_telefone"], v["empresa_nome"], v["atendente_nome"]) == (0, "", "Clínica Sorriso", "Carla")
        assert v["campanha_oferta"] == "" and v["economia_texto"] == "até 30%"
        assert db.scalar(select(func.count()).select_from(Blacklist)) == 0
    assert config_sistema.valores(db)["meta_diaria"] == 500


def test_disparo_roda_cada_cliente_com_os_proprios_numeros(db, server, evo, outro):
    from app.tasks.disparo import ciclo_disparo

    n_alfa, _, lead_alfa = montar(db, server, "Alfa", "+5519955550001")
    with no_cliente(db, outro.id):
        n_beta, _, lead_beta = montar(db, server, "Beta", "+5519955550001")

    import app.services.disparo as disparo_svc

    original = disparo_svc.ciclo
    disparo_svc.ciclo = lambda db, agora=None, rng=None: original(db, SEGUNDA_9H, random.Random(1))
    try:
        assert ciclo_disparo() == 2
    finally:
        disparo_svc.ciclo = original
    with SessionLocal() as s:
        envios = {(e.numero_id, e.lead_id, e.cliente_id) for e in s.scalars(select(Envio).where(Envio.status == EnvioStatus.ENVIADO))}
    assert envios == {(n_alfa.id, lead_alfa.id, CLIENTE_PADRAO), (n_beta.id, lead_beta.id, outro.id)}


def test_mensagem_recebida_vai_para_o_cliente_do_numero(client, db, server, outro):
    cadastrar_instancia(db, server, "chip-alfa")
    alfa = Lead(nome="Alfa", telefone="+5519911110001", whatsapp_jid="5519911110001@s.whatsapp.net", status=LeadStatus.ENVIADO)
    db.add(alfa)
    db.commit()
    with no_cliente(db, outro.id):
        cadastrar_instancia(db, server, "chip-beta")
        beta = Lead(nome="Beta", telefone="+5519911110001", whatsapp_jid="5519911110001@s.whatsapp.net", status=LeadStatus.ENVIADO)
        db.add(beta)
        db.commit()
    corpo = payload("oi, quero saber mais") | {"instance": "chip-beta"}
    assert client.post("/webhooks/evolution/test-webhook-token", json=corpo).json() == {"status": "ok"}
    with SessionLocal() as s:
        dono = s.scalars(select(Mensagem.lead_id).execution_options(**TODOS)).all()
    assert dono == [beta.id]
    assert client.post("/webhooks/evolution/test-webhook-token", json=payload("x", msg_id="M2") | {"instance": "desconhecida"}).json() == {"status": "ok"}


def test_criar_cliente_copiando_configuracoes_e_roteiros(db, outro):
    from agente_fakes import montar_docx

    config_sistema.salvar(db, {"meta_diaria": "800", "disparo_inicio": "08:00", "notificar_telefone": "(11) 98888-7777"})
    roteiros.importar(db, roteiros.ler_docx(montar_docx()))
    db.commit()
    origem = db.get(Cliente, CLIENTE_PADRAO)
    copia = clientes.criar(db, nome="Hotel Mar", copiar_de=origem, copiar_roteiros=True)
    db.commit()
    with no_cliente(db, copia.id):
        v = config_sistema.valores(db)
        assert (v["meta_diaria"], v["disparo_inicio"], v["empresa_nome"], v["notificar_telefone"]) == (800, "08:00", "Hotel Mar", "")
        assert db.scalar(select(func.count()).select_from(Segmento)) == db.scalar(
            select(func.count()).select_from(Segmento).where(Segmento.cliente_id == CLIENTE_PADRAO).execution_options(**TODOS)
        )
    with pytest.raises(clientes.ClienteError, match="Já existe"):
        clientes.criar(db, nome="hotel mar")


def test_arquivar_para_tudo_e_some_do_seletor(logged_client, db, server, outro):
    with no_cliente(db, outro.id):
        _, campanha, _ = montar(db, server, "Beta", "+5519955550001")
    logged_client.post("/cliente/trocar", data={"cliente_id": str(outro.id)})
    assert "ok=" in logged_client.post(f"/clientes/{outro.id}/arquivar").headers["location"]
    with SessionLocal() as s:
        assert s.get(Campanha, campanha.id).status == CampanhaStatus.PAUSADA
        assert s.get(Cliente, outro.id).ativo is False
    pagina = logged_client.get("/").text  # volta para o cliente ativo
    assert ">Clínica Sorriso</option>" not in pagina and "selected>Brafort10" in pagina
    from app.tenancy import clientes_ativos

    with SessionLocal() as s:  # sessão curta: uma leitura aberta travaria o SQLite para a próxima requisição
        assert clientes_ativos(s) == [CLIENTE_PADRAO]
    logged_client.post(f"/clientes/{outro.id}/reativar")
    assert ">Clínica Sorriso</option>" in logged_client.get("/").text


def test_excluir_apaga_so_o_cliente_e_desconecta_os_numeros(logged_client, db, server, evo, outro):
    montar(db, server, "Alfa", "+5519955550001")
    with no_cliente(db, outro.id):
        n_beta, _, _ = montar(db, server, "Beta", "+5519955550001")
    errado = logged_client.post(f"/clientes/{outro.id}/excluir", data={"confirmacao": "outro nome"})
    assert "erro=" in errado.headers["location"]
    resp = logged_client.post(f"/clientes/{outro.id}/excluir", data={"confirmacao": "clínica sorriso"})
    assert "ok=" in resp.headers["location"]
    assert ("delete_instance", n_beta.instance_name) in evo.calls
    with SessionLocal() as s:
        assert s.get(Cliente, outro.id) is None
        for modelo in (WhatsappNumber, Campanha, Lead, Empresa, Busca, Vendedor, Segmento):
            donos = set(s.scalars(select(modelo.cliente_id).execution_options(**TODOS)))
            assert donos == {CLIENTE_PADRAO}, modelo.__name__
    assert "Alfa Padaria" in logged_client.get("/empresas").text


def test_sem_cliente_ativo_so_abre_a_tela_de_clientes(logged_client, db):
    db.get(Cliente, CLIENTE_PADRAO).ativo = False
    db.commit()
    assert logged_client.get("/empresas").headers["location"] == "/clientes"
    assert "Nenhum cliente ativo" in logged_client.get("/clientes").text
    resp = logged_client.post("/clientes", data={"nome": "Novo Cliente"})
    assert resp.headers["location"] == "/numeros"
    assert logged_client.get("/numeros").status_code == 200


def test_registro_sem_cliente_e_recusado():
    from app.tenancy import SemCliente

    with SessionLocal() as s, pytest.raises(SemCliente):
        s.add(Vendedor(nome="Sem dono", ativo=True))
