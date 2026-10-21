import random
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import Autor, Busca, BuscaStatus, Direcao, Envio, EnvioStatus, EnvioTipo, Lead, LeadStatus, Mensagem
from app.services import buscas, campanhas, disparo, historico, numeros
from app.services.agenda import utc
from tests.test_disparo import SEGUNDA_9H, criar_campanha, criar_numero


@pytest.fixture(autouse=True)
def sem_virada(monkeypatch):
    monkeypatch.setattr(numeros, "garantir_virada", lambda db, agora=None: False)


def lead(db, campanha, telefone, status=LeadStatus.NA_FILA, **extra):
    novo = Lead(nome=f"Empresa {telefone[-4:]}", telefone=telefone, status=status,
                campanha_id=campanha.id if campanha else None, whatsapp_jid=f"{telefone[1:]}@s.whatsapp.net", **extra)
    db.add(novo)
    db.commit()
    return novo


@pytest.mark.parametrize("historia", ["envio_antigo", "conversa", "mesmo_whatsapp", "chip_proprio"])
def test_disparo_pula_quem_ja_foi_prospectado(db, server, evo, historia):
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    velho = lead(db, c, "+5519955550001")
    if historia == "envio_antigo":
        db.add(Envio(lead_id=velho.id, numero_id=n.id, tipo=EnvioTipo.ABERTURA, texto="oi", status=EnvioStatus.ENVIADO,
                     enviado_em=utc(SEGUNDA_9H - timedelta(days=60))))
    elif historia == "conversa":
        db.add(Mensagem(lead_id=velho.id, numero_id=n.id, direcao=Direcao.ENTRADA, autor=Autor.LEAD, texto="oi"))
    elif historia == "mesmo_whatsapp":
        # o mesmo WhatsApp, cadastrado com o telefone sem o 9, já foi abordado
        db.add(Lead(nome="Antigo", telefone="+551955550001", status=LeadStatus.SEM_RESPOSTA,
                    whatsapp_jid=velho.whatsapp_jid, enviado_em=utc(SEGUNDA_9H - timedelta(days=90))))
    else:
        n.telefone = velho.telefone
    db.commit()
    novo = lead(db, c, "+5519955550002")

    assert disparo.ciclo(db, SEGUNDA_9H, random.Random(1)) == 1
    db.refresh(velho)
    assert (velho.status, velho.numero_id) == (LeadStatus.JA_PROSPECTADO, None)
    enviados = db.scalars(select(Envio.lead_id).where(Envio.status == EnvioStatus.ENVIADO, Envio.enviado_em >= utc(SEGUNDA_9H))).all()
    assert enviados == [novo.id]


def test_motivos(db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    virgem = lead(db, c, "+5519955550003")
    assert historico.motivo_ja_prospectado(db, virgem) is None
    virgem.enviado_em = utc(SEGUNDA_9H)
    assert historico.motivo_ja_prospectado(db, virgem) == "já recebeu mensagem antes"


def test_campanha_nao_recebe_quem_ja_foi_prospectado(db, server, evo):
    n = criar_numero(db, server, "Chip", 10)
    c = criar_campanha(db, [n])
    busca = Busca(termo="padaria", cidade="Campinas", limite=10, status=BuscaStatus.CONCLUIDA)
    db.add(busca)
    db.commit()
    livre = lead(db, None, "+5519955550004", status=LeadStatus.NOVO, busca_id=busca.id)
    devolvido = lead(db, None, "+5519955550005", status=LeadStatus.NOVO, busca_id=busca.id,
                     enviado_em=utc(SEGUNDA_9H - timedelta(days=30)))  # voltou de uma campanha encerrada
    duplicado = lead(db, None, "+5519955550006", status=LeadStatus.NOVO, busca_id=busca.id)
    db.add(Lead(nome="Antigo", telefone="+551955550006", status=LeadStatus.EM_CONVERSA, whatsapp_jid=duplicado.whatsapp_jid))
    db.commit()

    assert campanhas.adicionar_leads(db, c, [busca.id]) == 1
    db.commit()
    for l in (livre, devolvido, duplicado):
        db.refresh(l)
    assert (livre.status, livre.campanha_id) == (LeadStatus.NA_FILA, c.id)
    assert devolvido.status == duplicado.status == LeadStatus.JA_PROSPECTADO
    assert devolvido.campanha_id is None


def test_busca_descarta_o_mesmo_whatsapp_com_outro_telefone(db, server, evo, monkeypatch):
    criar_numero(db, server, "Chip", 10)
    antigo = Lead(nome="Antigo", telefone="+551955550007", status=LeadStatus.SEM_RESPOSTA, whatsapp_jid="551955550007@s.whatsapp.net")
    db.add(antigo)
    db.commit()
    # o WhatsApp devolve o mesmo jid (sem o 9) para o telefone com o 9
    mesmo_jid = {"5519955550007": "551955550007@s.whatsapp.net", "5519955550008": "551955550099@s.whatsapp.net",
                 "5519955550009": "551955550099@s.whatsapp.net"}
    monkeypatch.setattr(evo, "check_whatsapp", lambda name, nums: {x: mesmo_jid.get(x) for x in nums})

    from app.services import importacao
    from app.services.importacao import Contato

    busca = importacao.importar(db, [Contato(1, "19 95555-0007"), Contato(2, "19 95555-0008"), Contato(3, "19 95555-0009")], nome_lista="Lista")
    db.commit()
    buscas.reivindicar_processamento(db, busca.id)
    buscas.processar(db, busca, pausa_entre_lotes=0)
    assert busca.total_novos == 1
    assert busca.descartes == {"ja_era_lead": 1, "repetido_na_busca": 1}


def test_lead_de_teste_pode_ser_reiniciado(db, server, evo):
    from app.services import lead_teste

    n = criar_numero(db, server, "Chip", 10)
    n.telefone = "+5511999990001"
    c = criar_campanha(db, [n])
    lead_teste.criar(db, nome="Teste", telefone="19 95555-0010", numero=n, campanha=c)
    db.commit()
    l = lead_teste.criar(db, nome="Teste", telefone="19 95555-0010", numero=n, campanha=c)
    assert l.status == LeadStatus.ENVIADO
