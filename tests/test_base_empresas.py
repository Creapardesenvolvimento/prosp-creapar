import io
import random
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import Busca, BuscaStatus, Empresa, Lead, LeadStatus
from app.services import base_empresas, buscas, campanhas, config_sistema, disparo, followup, importacao, numeros
from app.services.agenda import utc
from app.services.base_empresas import Filtros
from app.services.importacao import Contato
from tests.test_disparo import SEGUNDA_9H, criar_campanha, criar_numero

CABECALHO = ["Id", "GMapsLink", "OrganizationName", "OrganizationGMapsId", "RateStars", "ReviewsTotalCount",
             "OrganizationCategory", "OrganizationAddress", "OrganizationWebsite", "OrganizationPhoneNr",
             "OrganizationEmail", "OrganizationInstagram", "SearchRequest", "OrganizationPhoneFromTheWebsite"]


@pytest.fixture(autouse=True)
def sem_virada(monkeypatch):
    monkeypatch.setattr(numeros, "garantir_virada", lambda db, agora=None: False)


def planilha_do_extrator(linhas) -> bytes:
    import openpyxl

    livro = openpyxl.Workbook()
    livro.active.title = "Organizations"
    livro.active.append(CABECALHO)
    for linha in linhas:
        livro.active.append(linha)
    reviews = livro.create_sheet("Reviews")  # outra aba, com outro cabeçalho: não vira empresa
    reviews.append(["OrganizationId", "ReviewId", "ReviewerName", "ReviewerProfileLink", "ReviewerReviewsCount", "ReviewDate",
                    "ReviewRateStars", "ReviewTextOriginal", "ReviewTestTranslated", "ReviewerAvatarURL", "ReviewPhotosFiles",
                    "ReviewPhotosURLs", "OrganizationGMapsId", "OrganizationGUID"])
    arquivo = io.BytesIO()
    livro.save(arquivo)
    return arquivo.getvalue()


def test_le_a_planilha_do_extrator_do_google_maps():
    conteudo = planilha_do_extrator([
        [2, "https://maps.google.com/a", "Pousada do Preto", "1s0xabc", "4,7", "", "",
         "Endereço: Parque Estadual de Ilha Grande, Parque da - Bananal, Angra dos Reis - RJ", "pousadadopreto.com.br",
         "(24) 99968-9310", "abc@sentry.wixpress.com, contato@pousadadopreto.com.br", "https://instagram.com/preto,https://x",
         "angra+dos+reis-rj+pousada", ""],
        [3, "https://maps.google.com/b", "Pousada dos Sonhos", "1s0xdef", "4,2", "35", "Pousada",
         "Endereço: R. Maria José, 255 - Parque das Palmeiras, Angra dos Reis - RJ, 23906-445", "", "", "", "",
         "angra+dos+reis-rj+pousada", "(24) 3365-7806"],
        [4, "https://maps.google.com/b", "Pousada dos Sonhos (repetida)", "1s0xdef", "", "", "", "", "", "(24) 3365-7806", "", "", "", ""],
    ])
    contatos = importacao.contatos_do_arquivo("exported.xlsx", conteudo)
    preto, sonhos, _ = contatos
    assert (preto.nome, preto.telefone_original, preto.cidade, preto.estado) == ("Pousada do Preto", "(24) 99968-9310", "Angra dos Reis", "RJ")
    assert (preto.email, preto.nota, preto.site, preto.place_id) == ("contato@pousadadopreto.com.br", 4.7, "pousadadopreto.com.br", "1s0xabc")
    assert preto.extras == {"instagram": "https://instagram.com/preto", "busca": "angra dos reis-rj pousada"}
    assert preto.endereco.startswith("Parque Estadual")
    assert (sonhos.telefone_original, sonhos.avaliacoes, sonhos.categoria) == ("(24) 3365-7806", 35, "Pousada")


def test_importar_salva_tudo_na_empresa(db, server, evo):
    criar_numero(db, server, "Chip", 10)
    contatos = [
        Contato(1, "(24) 99968-9310", "Pousada do Preto", "Angra dos Reis", "", "RJ", site="pousadadopreto.com.br",
                email="contato@pousadadopreto.com.br", nota=4.7, place_id="p1", extras={"busca": "angra dos reis-rj pousada"}),
        Contato(2, "(24) 99968-9311", "Repetida", place_id="p1"),
    ]
    busca = importacao.importar(db, contatos, nome_lista="Angra")
    db.commit()
    empresa, = db.scalars(select(Empresa)).all()  # a repetida pelo id do Maps não entra
    assert (empresa.estado, empresa.site, empresa.place_id, empresa.categoria) == ("RJ", "pousadadopreto.com.br", "p1", "angra dos reis-rj pousada")
    buscas.reivindicar_processamento(db, busca.id)
    buscas.processar(db, busca, pausa_entre_lotes=0)
    lead = db.scalar(select(Lead))
    assert (lead.email, lead.site, lead.nota) == ("contato@pousadadopreto.com.br", "pousadadopreto.com.br", 4.7)


def importar_lista(db, nome, telefones):
    busca = importacao.importar(db, [Contato(i, t, f"Empresa {t[-4:]}") for i, t in enumerate(telefones)], nome_lista=nome)
    db.commit()
    buscas.reivindicar_processamento(db, busca.id)
    buscas.processar(db, busca, pausa_entre_lotes=0)
    db.commit()
    return busca


def test_base_sabe_quem_ja_foi_prospectado(db, server, evo):
    criar_numero(db, server, "Chip", 10)
    evo.sem_whatsapp = {"5524955550009"}
    primeira = importar_lista(db, "Lista 1", ["24 95555-0001", "24 95555-0002", "24 95555-0003", "24 95555-0004", "24 95555-0009"])
    leads = {l.telefone[-4:]: l for l in db.scalars(select(Lead))}
    leads["0001"].status, leads["0001"].enviado_em = LeadStatus.ENVIADO, utc(SEGUNDA_9H)
    leads["0002"].status, leads["0002"].followups_enviados = LeadStatus.ENVIADO, 2
    leads["0003"].status = LeadStatus.SEM_RESPOSTA
    db.commit()

    # Subo a lista de novo, com uma empresa nova: as antigas aparecem com a situação de hoje.
    segunda = importar_lista(db, "Lista 2", ["24 95555-0001", "24 95555-0002", "24 95555-0003", "24 95555-0005"])
    assert segunda.total_novos == 1 and segunda.descartes == {"ja_era_lead": 3}
    assert base_empresas.contagens(db, Filtros(lista=segunda.id)) == {
        "aguardando": 1, "followup": 1, "nem_respondeu": 1, "nao_contatado": 1,
    }
    # Na base geral, cada empresa aparece uma vez só.
    geral = base_empresas.contagens(db, Filtros())
    assert geral == {"aguardando": 1, "followup": 1, "nem_respondeu": 1, "nao_contatado": 2, "sem_whatsapp": 1}
    linhas, total = base_empresas.listar(db, Filtros(situacao="nao_contatado"))
    assert total == 2 and {e.telefone[-4:] for e, _, _ in linhas} == {"0004", "0005"}
    assert all(not s.contatado for _, _, s in linhas)
    linhas, _ = base_empresas.listar(db, Filtros(texto="95555-0002"))
    assert [s.rotulo for _, _, s in linhas] == ["Em follow-up"]


def test_nao_contatados_vao_para_a_campanha(logged_client, db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    lista = importar_lista(db, "Lista", ["24 95555-0001", "24 95555-0002"])
    contatado = db.scalar(select(Lead).where(Lead.telefone == "+5524955550001"))
    contatado.status, contatado.enviado_em = LeadStatus.SEM_RESPOSTA, utc(SEGUNDA_9H)
    db.commit()

    pagina = logged_client.get(f"/empresas?lista={lista.id}").text
    assert "Nem respondeu" in pagina and "Não contatado" in pagina and "Colocar na fila" in pagina
    resp = logged_client.post("/empresas/para-campanha", data={"campanha_id": str(c.id), "lista": str(lista.id)})
    assert "ok=" in resp.headers["location"]
    from app.db import SessionLocal

    with SessionLocal() as s:
        status = dict(s.execute(select(Lead.telefone, Lead.status)).all())
    assert status == {"+5524955550001": LeadStatus.SEM_RESPOSTA, "+5524955550002": LeadStatus.NA_FILA}
    assert "Situação destas empresas na base" in logged_client.get(f"/buscas/{lista.id}").text
    planilha = logged_client.get(f"/empresas.csv?lista={lista.id}").text
    assert "Nem respondeu" in planilha and "Na fila de disparo" in planilha


def test_desqualificar_tira_da_prospeccao(logged_client, db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    lead = Lead(nome="Errado", telefone="+5524955550001", status=LeadStatus.NA_FILA, campanha_id=c.id)
    db.add(lead)
    db.commit()
    resp = logged_client.post(f"/leads/{lead.id}/desqualificar", data={"motivo": "não é empresa", "voltar": "/empresas"})
    assert resp.headers["location"] == "/empresas"
    db.refresh(lead)
    assert (lead.status, lead.ia_ativa, lead.handoff_motivo) == (LeadStatus.DESQUALIFICADO, False, "manual: não é empresa")
    assert disparo.ciclo(db, SEGUNDA_9H, random.Random(1)) == 0
    assert "Desqualificado" in logged_client.get(f"/leads/{lead.id}").text


def test_sem_followup_quem_nao_responde_vira_nem_respondeu(db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    lead = Lead(nome="x", telefone="+5524955550001", status=LeadStatus.ENVIADO, campanha_id=c.id, numero_id=n.id,
                enviado_em=utc(SEGUNDA_9H - timedelta(days=4)))
    novo = Lead(nome="y", telefone="+5524955550002", status=LeadStatus.ENVIADO, campanha_id=c.id, numero_id=n.id,
                enviado_em=utc(SEGUNDA_9H - timedelta(days=1)))
    db.add_all([lead, novo])
    config_sistema.salvar(db, {"followup_ativo": ""})
    db.commit()
    disparo.ciclo(db, SEGUNDA_9H, random.Random(1))
    db.refresh(lead)
    db.refresh(novo)
    assert (lead.status, novo.status) == (LeadStatus.SEM_RESPOSTA, LeadStatus.ENVIADO)


def test_tela_de_empresas_vazia_e_menu(logged_client):
    pagina = logged_client.get("/empresas").text
    assert "Nenhuma empresa ainda" in pagina and 'href="/empresas"' in pagina
