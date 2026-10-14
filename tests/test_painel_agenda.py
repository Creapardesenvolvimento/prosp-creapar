from app.db import SessionLocal
from app.models import Reuniao, ReuniaoStatus, Vendedor

FAIXAS_SEMANA = {f"faixas_{d}": "09:00-12:00, 14:00-18:00" for d in range(5)}


def criar_vendedor(logged_client, nome="Ana", **extra):
    data = {"nome": nome, "telefone": "(11) 97777-6666", "link_reuniao": "https://meet.google.com/x", "ativo": "on"}
    resp = logged_client.post("/vendedores", data={**data, **FAIXAS_SEMANA, **extra})
    assert resp.status_code == 303, resp.text
    return int(resp.headers["location"].rsplit("/", 1)[1])


def buscar(model, id_):
    with SessionLocal() as s:
        return s.get(model, id_)


def test_telas_exigem_login(client):
    for path in ("/vendedores", "/vendedores/novo", "/agenda"):
        assert client.get(path).headers["location"] == "/login"


def test_cadastrar_e_editar_vendedor(logged_client):
    vid = criar_vendedor(logged_client)
    pagina = logged_client.get(f"/vendedores/{vid}")
    assert 'value="09:00-12:00, 14:00-18:00"' in pagina.text
    assert "+5511977776666" in pagina.text

    resp = logged_client.post(f"/vendedores/{vid}", data={"nome": "Ana Paula", "faixas_5": "09:00-12:00"})
    assert resp.status_code == 303
    v = buscar(Vendedor, vid)
    assert (v.nome, v.ativo, v.telefone) == ("Ana Paula", False, None)
    assert "sábado" in logged_client.get("/vendedores").text


def test_erro_de_validacao_mantem_o_formulario(logged_client):
    resp = logged_client.post("/vendedores", data={"nome": "Ana", "faixas_0": "18:00-09:00", "ativo": "on"})
    assert resp.status_code == 400
    assert "início precisa ser antes do fim" in resp.text
    assert 'value="18:00-09:00"' in resp.text


def test_bloqueio_pelo_painel(logged_client):
    vid = criar_vendedor(logged_client)
    logged_client.post(
        f"/vendedores/{vid}/bloqueios", data={"inicio": "2030-01-06T09:00", "fim": "2030-01-10T18:00", "motivo": "Férias"}
    )
    pagina = logged_client.get(f"/vendedores/{vid}")
    assert "06/01 09:00 até 10/01 18:00" in pagina.text and "Férias" in pagina.text


def test_agendar_manual_e_avisar(logged_client, evo):
    criar_vendedor(logged_client)
    pagina = logged_client.get("/agenda")
    assert "A IA ofereceria agora" in pagina.text

    select_inicio = pagina.text.split('name="inicio"', 1)[1]
    inicio = select_inicio.split('<option value="', 1)[1].split('"', 1)[0]
    resp = logged_client.post("/agenda", data={"inicio": inicio, "lead_nome": "Padaria X"})
    assert resp.headers["location"] == "/agenda"

    with SessionLocal() as s:
        reuniao = s.query(Reuniao).one()
        assert reuniao.status == ReuniaoStatus.AGENDADA
        reuniao_id = reuniao.id
    assert "Padaria X" in logged_client.get("/agenda").text
    # nenhum número conectado nos testes: o aviso fica pendente
    assert evo.called("send_text") == []

    resp = logged_client.post("/agenda", data={"inicio": inicio, "lead_nome": "Outro"})
    assert "erro=" in resp.headers["location"]

    logged_client.post(f"/reunioes/{reuniao_id}/cancelar")
    assert buscar(Reuniao, reuniao_id).status == ReuniaoStatus.CANCELADA


def test_agenda_sem_vendedores(logged_client):
    assert "Nenhum horário livre" in logged_client.get("/agenda").text
