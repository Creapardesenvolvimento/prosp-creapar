from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Busca, BuscaStatus, Empresa, Lead, LeadStatus
from app.services import buscas as svc

WEBHOOK = "/webhooks/apify/test-webhook-token"
ITENS = [
    {"title": "Pousada Boa", "phoneUnformatted": "+5524999990001", "website": "https://boa.com.br", "categoryName": "Pousada",
     "address": "Rua A, 1", "neighborhood": "Centro", "city": "Angra dos Reis", "state": "Rio de Janeiro",
     "totalScore": 4.7, "reviewsCount": 120, "placeId": "p1", "url": "https://maps.google.com/?cid=1", "openingHours": [{"day": "seg"}]},
    {"title": "=Hotel Fixo", "phoneUnformatted": "+552433334444", "placeId": "p2", "city": "Angra dos Reis"},
    {"title": "Sem telefone", "placeId": "p3"},
    {"title": "Pousada Boa (repetida)", "phoneUnformatted": "+5524999990001", "placeId": "p1"},  # mesmo lugar
]


def criar(logged_client, **extra):
    resp = logged_client.post("/buscas", data={"termo": "pousada", "cidade": "Angra dos Reis, RJ", "limite": 20, **extra})
    assert resp.status_code == 303, resp.text
    return int(resp.headers["location"].rsplit("/", 1)[1])


def empresas(busca_id):
    with SessionLocal() as s:
        return s.scalars(select(Empresa).where(Empresa.busca_id == busca_id).order_by(Empresa.id)).all()


def test_guarda_todas_as_empresas_com_o_motivo(logged_client, client, conectado, apify_fake):
    apify_fake.items = ITENS
    busca_id = criar(logged_client)
    client.post(WEBHOOK, json={"eventType": "ACTOR.RUN.SUCCEEDED", "resource": {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "ds1"}})

    boa, fixo, sem = empresas(busca_id)  # o lugar repetido (mesmo placeId) entra uma vez só
    assert (boa.descarte, fixo.descarte, sem.descarte) == (None, "nao_celular", "sem_telefone")
    assert boa.lead_id is not None
    assert (boa.site, boa.bairro, boa.estado, boa.nota, boa.url_maps) == (
        "https://boa.com.br", "Centro", "Rio de Janeiro", 4.7, "https://maps.google.com/?cid=1"
    )
    assert boa.dados["openingHours"] == [{"day": "seg"}]  # o item completo do Apify fica guardado
    assert (fixo.telefone_original, fixo.telefone, fixo.celular) == ("+552433334444", "+552433334444", False)


def test_dados_ficam_guardados_mesmo_se_a_validacao_falhar(logged_client, client, server, apify_fake):
    apify_fake.items = ITENS
    busca_id = criar(logged_client)
    client.post(WEBHOOK, json={"eventType": "ACTOR.RUN.SUCCEEDED", "resource": {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "ds1"}})
    with SessionLocal() as s:
        assert s.get(Busca, busca_id).status == BuscaStatus.ERRO  # sem número conectado
    assert len(empresas(busca_id)) == 3


def test_reprocessar_usa_o_banco_e_nao_o_apify(db, conectado, apify_fake):
    busca = svc.criar_busca(db, "pousada", "Angra", 10)
    busca.status, busca.apify_dataset_id = BuscaStatus.PROCESSANDO, "ds1"
    apify_fake.items = ITENS
    svc.guardar_resultados(db, busca)
    db.commit()

    apify_fake.items = []  # o dataset expirou no Apify
    apify_fake.calls.clear()
    svc.processar(db, busca, pausa_entre_lotes=0)
    assert (busca.total_encontrados, busca.total_novos) == (3, 1)
    assert apify_fake.calls == []


def test_planilha_csv(logged_client, client, conectado, apify_fake):
    apify_fake.items = ITENS
    busca_id = criar(logged_client)
    client.post(WEBHOOK, json={"eventType": "ACTOR.RUN.SUCCEEDED", "resource": {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "ds1"}})

    resp = logged_client.get(f"/buscas/{busca_id}/empresas.csv")
    assert resp.headers["content-type"].startswith("text/csv")
    assert f"pousada-angra-dos-reis-rj-{busca_id}.csv" in resp.headers["content-disposition"]
    texto = resp.content.decode("utf-8-sig")
    linhas = texto.strip().splitlines()
    assert linhas[0].startswith("Empresa;Telefone;Telefone (padronizado);Celular;Situação")
    assert linhas[1].startswith("Pousada Boa;'+5524999990001;'+5524999990001;sim;virou lead;Pousada;https://boa.com.br")
    assert ";4,7;120;" in linhas[1]
    assert linhas[2].startswith("'=Hotel Fixo;")  # nome que viraria fórmula no Excel
    assert "telefone fixo" in linhas[2]
    assert "sem telefone" in linhas[3]

    pagina = logged_client.get(f"/buscas/{busca_id}")
    assert "Baixar planilha com todas as 3 empresas" in pagina.text
    assert "Empresas descartadas (2)" in pagina.text and "=Hotel Fixo" in pagina.text


def test_avisa_antes_de_repetir_a_busca(logged_client, client, conectado, apify_fake):
    apify_fake.items = ITENS
    primeira = criar(logged_client)
    client.post(WEBHOOK, json={"eventType": "ACTOR.RUN.SUCCEEDED", "resource": {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "ds1"}})

    resp = logged_client.post("/buscas", data={"termo": "Pousada", "cidade": "angra dos reis, rj", "limite": 20})
    assert resp.status_code == 409
    assert "Você já fez essa busca recentemente" in resp.text and f"/buscas/{primeira}" in resp.text
    assert sum(1 for c in apify_fake.calls if c[0] == "start_run") == 1  # o aviso não gastou nada

    segunda = criar(logged_client, confirmar="1")
    assert segunda != primeira


def test_recuperar_empresas_de_busca_antiga(db, apify_fake):
    busca = Busca(termo="pousada", cidade="Angra", limite=10, status=BuscaStatus.CONCLUIDA, apify_dataset_id="ds1",
                  total_encontrados=3, total_novos=1, descartes={})
    db.add(busca)
    db.flush()
    db.add(Lead(busca_id=busca.id, nome="Pousada Boa", telefone="+5524999990001", status=LeadStatus.NOVO))
    db.commit()
    apify_fake.items = ITENS + [{"title": "Celular sem WhatsApp", "phoneUnformatted": "+5524999990009", "placeId": "p9"}]

    assert svc.recuperar_empresas(db, busca) == 4
    situacao = {e.nome: (e.descarte, e.lead_id is not None) for e in busca_empresas(db, busca)}
    assert situacao == {
        "Pousada Boa": (None, True),
        "=Hotel Fixo": ("nao_celular", False),
        "Sem telefone": ("sem_telefone", False),
        "Celular sem WhatsApp": ("sem_whatsapp", False),
    }
    assert db.scalar(select(func.count()).select_from(Lead)) == 1  # não cria leads de novo


def busca_empresas(db, busca):
    return db.scalars(select(Empresa).where(Empresa.busca_id == busca.id).order_by(Empresa.id)).all()


def test_recuperar_quando_o_apify_ja_apagou(logged_client, db, apify_fake):
    from app.integrations.apify import ApifyError

    busca = Busca(termo="pousada", cidade="Angra", limite=10, status=BuscaStatus.CONCLUIDA, apify_dataset_id="ds1", descartes={})
    db.add(busca)
    db.commit()
    assert "Recuperar empresas do Apify" in logged_client.get(f"/buscas/{busca.id}").text

    def expirou(dataset_id):
        raise ApifyError("Apify respondeu 404: dataset não encontrado")

    apify_fake.dataset_items = expirou
    resp = logged_client.post(f"/buscas/{busca.id}/recuperar")
    assert "expiram" in logged_client.get(resp.headers["location"]).text
