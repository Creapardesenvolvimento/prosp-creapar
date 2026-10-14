from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from agente_fakes import FakeIA, ferramenta, montar_docx, resposta, texto
from app.models import (
    Autor,
    Blacklist,
    Direcao,
    Envio,
    EnvioTipo,
    Etapa,
    Lead,
    LeadStatus,
    Mensagem,
    Reuniao,
    Segmento,
)
from app.services import agenda, agente, ia, roteiros
from app.services.ia import IAError


@pytest.fixture
def fake_ia(monkeypatch):
    fake = FakeIA()
    monkeypatch.setattr(ia, "get_client", lambda: fake)
    return fake


@pytest.fixture
def segmentos(db):
    roteiros.importar(db, roteiros.ler_docx(montar_docx()))
    db.commit()


@pytest.fixture
def lead(db, conectado, segmentos):
    lead = Lead(
        nome="Pousada Sol", telefone="+5524999990001", whatsapp_jid="5524999990001@s.whatsapp.net",
        categoria="Pousada", cidade="Angra dos Reis", avaliacoes=40, status=LeadStatus.RESPONDEU, numero_id=conectado.id,
    )
    db.add(lead)
    db.flush()
    db.add(Mensagem(lead_id=lead.id, numero_id=conectado.id, direcao=Direcao.SAIDA, autor=Autor.SISTEMA, texto="Olá, bom dia! Falo com a Pousada Sol?"))
    db.add(Mensagem(lead_id=lead.id, numero_id=conectado.id, direcao=Direcao.ENTRADA, autor=Autor.LEAD, texto="Bom dia, sim"))
    db.commit()
    return lead


def classificacao(porte="pequeno", segmento="Pousadas"):
    return resposta(ferramenta("classificar_lead", {"segmento": segmento, "porte": porte}))


# --- roteiros ------------------------------------------------------------------------


def test_importa_docx_e_preenche_campos(db, segmentos):
    pousadas = db.scalar(select(Segmento).where(Segmento.nome == "Pousadas"))
    assert len(pousadas.roteiros) == 8  # pequeno e grande, 4 etapas cada
    assert "hotel" in pousadas.palavras_chave  # palavras-chave iniciais do segmento conhecido
    lead = Lead(nome="x", telefone="+5524999990009", segmento="Pousadas", porte="pequeno")
    r = roteiros.roteiro_do_lead(db, lead)
    assert r["foco"] == "Produto Concentrado + Economia"
    assert r["etapas"]["atencao"].startswith("Olá! Meu nome é Bruna e sou da Brafort10")
    assert "até 50%" in r["etapas"]["desejo"]
    # porte sem roteiro próprio cai no pequeno
    lead.porte = "medio"
    assert roteiros.roteiro_do_lead(db, lead)["porte"] == "pequeno"


def test_reimportar_substitui_textos_e_mantem_palavras(db, segmentos):
    pousadas = db.scalar(select(Segmento).where(Segmento.nome == "Pousadas"))
    pousadas.palavras_chave = "pousada, chalé"
    db.commit()
    roteiros.importar(db, roteiros.ler_docx(montar_docx([("Heading2", "Segmento: Pousadas"), ("Heading3", "1. Pequeno Porte"), (None, "Ação: Novo texto")])))
    db.commit()
    db.refresh(pousadas)
    assert [r.texto for r in pousadas.roteiros] == ["Novo texto"]
    assert pousadas.palavras_chave == "pousada, chalé"


def test_docx_invalido():
    with pytest.raises(roteiros.RoteiroError):
        roteiros.ler_docx(b"nao e docx")
    with pytest.raises(roteiros.RoteiroError):
        roteiros.ler_docx(montar_docx([(None, "Só um texto qualquer")]))


def test_segmento_por_palavras(db, segmentos):
    assert roteiros.segmento_por_palavras(db, "Hotel fazenda").nome == "Pousadas"
    assert roteiros.segmento_por_palavras(db, "Lavanderia self-service").nome == "Lavanderias"
    assert roteiros.segmento_por_palavras(db, "Sorveteria") is None


# --- classificação ---------------------------------------------------------------------


def test_classifica_segmento_por_palavras_e_porte_pela_ia(db, lead, fake_ia):
    fake_ia.fila.append(classificacao(porte="grande", segmento="qualquer"))
    agente.classificar(db, lead)
    assert (lead.segmento, lead.porte) == ("Pousadas", "grande")
    assert "O segmento já foi definido como \"Pousadas\"" in fake_ia.chamadas[0]["messages"][0]["content"]


def test_ia_escolhe_o_segmento_quando_as_palavras_nao_batem(db, lead, fake_ia):
    lead.categoria = "Spa e estância"
    fake_ia.fila.append(classificacao(porte="medio", segmento="Pousadas"))
    agente.classificar(db, lead)
    assert (lead.segmento, lead.porte) == ("Pousadas", "medio")


def test_sem_ia_o_porte_vem_das_avaliacoes(db, lead, fake_ia):
    fake_ia.erro = IAError("fora do ar")
    lead.avaliacoes = 650
    agente.classificar(db, lead)
    assert (lead.segmento, lead.porte) == ("Pousadas", "grande")
    assert agente.porte_por_avaliacoes(None) == "pequeno" and agente.porte_por_avaliacoes(150) == "medio"


# --- conversa ---------------------------------------------------------------------------


def test_primeira_resposta_segue_a_etapa_atencao(db, lead, fake_ia, evo):
    fake_ia.fila += [classificacao(), resposta(ferramenta("responder", {"mensagem": "Olá! Sou a Bruna, da Brafort10. Vocês buscam economia na limpeza?", "etapa": "atencao"}))]

    assert agente.conversar(db, lead) == "respondeu"

    sistema = fake_ia.chamadas[1]["system"]
    assert "Você é Bruna, da Brafort10" in sistema
    assert "Atenção  ← PRÓXIMA" in sistema and "Meu nome é Bruna" in sistema and "até 50%" in sistema
    assert fake_ia.chamadas[1]["messages"] == [{"role": "user", "content": "Bom dia, sim"}]
    (_, instance, number, enviado), = evo.called("send_text")
    assert number == "5524999990001" and enviado.startswith("Olá! Sou a Bruna")
    assert (lead.etapa, lead.status) == (Etapa.INTERESSE, LeadStatus.EM_CONVERSA)
    envio = db.scalar(select(Envio).where(Envio.tipo == EnvioTipo.IA))
    assert envio.evolution_message_id and agente.eh_do_sistema(db, envio.evolution_message_id)
    assert db.scalar(select(Mensagem).where(Mensagem.autor == Autor.IA)).texto == enviado


def test_responder_duvida_nao_avanca_a_etapa(db, lead, fake_ia, evo):
    lead.segmento, lead.porte, lead.etapa = "Pousadas", "pequeno", Etapa.INTERESSE
    fake_ia.fila.append(resposta(ferramenta("responder", {"mensagem": "São produtos para limpeza geral.", "etapa": "conversa"})))
    agente.conversar(db, lead)
    assert lead.etapa == Etapa.INTERESSE


def test_resposta_em_texto_sem_ferramenta_tambem_sai(db, lead, fake_ia, evo):
    lead.segmento, lead.porte = "Pousadas", "pequeno"
    fake_ia.fila.append(resposta(texto("Olá! Tudo bem?")))
    assert agente.conversar(db, lead) == "respondeu"
    assert lead.etapa == Etapa.INTERESSE


def test_historico_junta_mensagens_e_marca_o_time(db, lead):
    db.add_all([
        Mensagem(lead_id=lead.id, direcao=Direcao.ENTRADA, autor=Autor.LEAD, texto="qual o preço?"),
        Mensagem(lead_id=lead.id, direcao=Direcao.SAIDA, autor=Autor.HUMANO, texto="te mando a tabela"),
        Mensagem(lead_id=lead.id, direcao=Direcao.ENTRADA, autor=Autor.LEAD, tipo="audio", texto=""),
    ])
    db.commit()
    abertura, msgs = agente._historico(db, lead)
    assert abertura == "Olá, bom dia! Falo com a Pousada Sol?"
    assert msgs == [
        {"role": "user", "content": "Bom dia, sim\nqual o preço?"},
        {"role": "assistant", "content": "(mensagem de um colega do time) te mando a tabela"},
        {"role": "user", "content": "[audio]"},
    ]


def test_sem_mensagem_nova_do_lead_nao_chama_a_ia(db, lead, fake_ia):
    lead.segmento, lead.porte = "Pousadas", "pequeno"
    db.add(Mensagem(lead_id=lead.id, direcao=Direcao.SAIDA, autor=Autor.IA, texto="Olá!"))
    db.commit()
    assert agente.conversar(db, lead) == "nada" and fake_ia.chamadas == []


def test_agendamento_pela_ia(db, lead, fake_ia, evo):
    agenda.salvar_vendedor(db, None, nome="Ana", telefone="", email="", link_reuniao="https://meet.google.com/x",
                           ativo=True, faixas_por_dia={d: "08:00-20:00" for d in range(7)})
    db.commit()
    lead.segmento, lead.porte, lead.etapa = "Pousadas", "pequeno", Etapa.ACAO
    escolhido = {}

    def escolher(kwargs):
        # o modelo leu o resultado de consultar_horarios e escolhe o primeiro
        import json

        resultado = json.loads(kwargs["messages"][-1]["content"][0]["content"])
        escolhido["inicio"] = resultado["horarios"][0]["inicio"]
        return resposta(ferramenta("agendar_reuniao", {"inicio": escolhido["inicio"], "observacoes": "quer economizar"}))

    fake_ia.fila += [
        resposta(ferramenta("consultar_horarios", {})),
        escolher,
        resposta(ferramenta("responder", {"mensagem": "Marcado! Te espero lá.", "etapa": "acao"})),
    ]
    assert agente.conversar(db, lead) == "respondeu"
    reuniao = db.scalar(select(Reuniao))
    assert (reuniao.lead_id, reuniao.observacoes, reuniao.link) == (lead.id, "quer economizar", "https://meet.google.com/x")
    assert lead.status == LeadStatus.REUNIAO_AGENDADA
    assert db.info["reunioes_novas"] == [reuniao.id]
    assert "Marcado!" in evo.called("send_text")[-1][3]


def test_agendamento_em_horario_ocupado_volta_para_a_ia(db, lead, fake_ia, evo):
    lead.segmento, lead.porte = "Pousadas", "pequeno"
    fake_ia.fila += [
        resposta(ferramenta("agendar_reuniao", {"inicio": "2030-01-01T10:00:00-03:00"})),
        lambda kw: resposta(ferramenta("responder", {"mensagem": "Esse horário não deu, posso ver outro?", "etapa": "conversa"}))
        if "Não foi possível agendar" in kw["messages"][-1]["content"][0]["content"] else None,
    ]
    assert agente.conversar(db, lead) == "respondeu"
    assert db.scalar(select(func.count()).select_from(Reuniao)) == 0


def test_atualizar_porte_troca_o_roteiro(db, lead, fake_ia, evo):
    lead.segmento, lead.porte = "Pousadas", "pequeno"
    fake_ia.fila += [
        resposta(ferramenta("atualizar_porte", {"porte": "grande", "motivo": "tem 4 unidades"})),
        lambda kw: resposta(ferramenta("responder", {"mensagem": "Entendi, 4 unidades!", "etapa": "atencao"}))
        if "Treinamento certificado" in kw["messages"][-1]["content"][0]["content"] else None,
    ]
    agente.conversar(db, lead)
    assert lead.porte == "grande"


def test_passar_para_humano(db, lead, fake_ia, evo):
    lead.segmento, lead.porte = "Pousadas", "pequeno"
    fake_ia.fila.append(resposta(ferramenta("passar_para_humano", {"motivo": "pediu tabela de preços", "mensagem_ao_lead": "Vou chamar um colega!"})))
    assert agente.conversar(db, lead) == "humano"
    assert (lead.ia_ativa, lead.status, lead.handoff_motivo) == (False, LeadStatus.HUMANO_ASSUMIU, "pediu tabela de preços")
    assert evo.called("send_text")[-1][3] == "Vou chamar um colega!"
    assert not agente.pode_responder(lead)


def test_sem_interesse(db, lead, fake_ia, evo):
    lead.segmento, lead.porte = "Pousadas", "pequeno"
    fake_ia.fila.append(resposta(ferramenta("marcar_sem_interesse", {"motivo": "já tem fornecedor", "mensagem_de_despedida": "Obrigada!"})))
    assert agente.conversar(db, lead) == "sem_interesse"
    assert (lead.status, lead.ia_ativa) == (LeadStatus.SEM_INTERESSE, False)


def test_opt_out_pela_ia(db, lead, fake_ia, evo):
    lead.segmento, lead.porte = "Pousadas", "pequeno"
    fake_ia.fila.append(resposta(ferramenta("registrar_opt_out", {"motivo": "pediu para não mandar mais"})))
    assert agente.conversar(db, lead) == "opt_out"
    db.commit()
    assert lead.status == LeadStatus.OPT_OUT
    assert db.scalar(select(Blacklist.telefone)) == lead.telefone
    assert evo.called("send_text") == []


def test_ia_fora_do_ar_passa_para_humano(db, lead, fake_ia, evo):
    lead.segmento, lead.porte = "Pousadas", "pequeno"
    fake_ia.erro = IAError("Limite de uso da IA atingido.")
    assert agente.conversar(db, lead) == "humano"
    assert "IA indisponível" in lead.handoff_motivo and evo.called("send_text") == []


def test_ia_que_nao_termina_passa_para_humano(db, lead, fake_ia, evo):
    lead.segmento, lead.porte = "Pousadas", "pequeno"
    fake_ia.fila += [resposta(ferramenta("consultar_horarios", {}, id_=f"t{i}")) for i in range(agente.MAX_PASSOS)]
    assert agente.conversar(db, lead) == "humano"


def test_numero_desconectado_adia(db, lead, fake_ia, conectado):
    from app.services import numeros

    numeros.aplicar_estado(db, conectado, "close")
    assert agente.conversar(db, lead) == "nada" and fake_ia.chamadas == []


def test_devolver_para_ia(db, lead):
    agente.passar_para_humano(db, lead, "teste")
    agente.devolver_para_ia(db, lead)
    assert (lead.ia_ativa, lead.status, lead.handoff_motivo) == (True, LeadStatus.EM_CONVERSA, None)


# --- ritmo do pitch -------------------------------------------------------------------


def test_depois_da_atencao_o_pitch_sai_em_sequencia(db, lead, fake_ia, evo):
    lead.segmento, lead.porte, lead.etapa = "Pousadas", "pequeno", Etapa.INTERESSE
    fake_ia.fila.append(resposta(ferramenta("responder", {
        "mensagens": ["Na Brafort10 temos produtos super concentrados.", "Dá para reduzir até 50% dos gastos.", "Que tal uma análise gratuita?"],
        "etapa": "acao",
    })))
    assert agente.conversar(db, lead) == "respondeu"
    sistema = fake_ia.chamadas[0]["system"]
    assert sistema.count("← PRÓXIMAS (enviar juntas)") == 3 and "é a hora do pitch" in sistema
    assert [c[3] for c in evo.called("send_text")] == [
        "Na Brafort10 temos produtos super concentrados.", "Dá para reduzir até 50% dos gastos.", "Que tal uma análise gratuita?",
    ]
    assert lead.etapa == Etapa.ACAO
    assert db.scalar(select(func.count()).select_from(Mensagem).where(Mensagem.autor == Autor.IA)) == 3


def test_na_atencao_ainda_e_uma_mensagem_e_espera(db, lead, fake_ia, evo):
    lead.segmento, lead.porte = "Pousadas", "pequeno"
    fake_ia.fila.append(resposta(ferramenta("responder", {"mensagens": ["Oi! Sou a Bruna, da Brafort10."], "etapa": "atencao"})))
    agente.conversar(db, lead)
    sistema = fake_ia.chamadas[0]["system"]
    assert "Atenção  ← PRÓXIMA" in sistema and "entregue só a Atenção" in sistema and "hora do pitch" not in sistema
    assert lead.etapa == Etapa.INTERESSE


def test_trava_impede_adiantar_o_pitch_na_atencao(db, lead, fake_ia, evo):
    lead.segmento, lead.porte = "Pousadas", "pequeno"
    fake_ia.fila.append(resposta(ferramenta("responder", {"mensagens": ["Oi! Sou a Bruna.", "Temos produtos.", "Bora conversar?"], "etapa": "acao"})))
    agente.conversar(db, lead)
    assert [c[3] for c in evo.called("send_text")] == ["Oi! Sou a Bruna."]
    assert lead.etapa == Etapa.INTERESSE  # o pitch fica para depois da resposta do lead


def test_atencao_em_duas_partes_sai_inteira(db, lead, fake_ia, evo):
    lead.segmento, lead.porte = "Pousadas", "pequeno"
    fake_ia.fila.append(resposta(ferramenta("responder", {"mensagens": ["Oi! Sou a Bruna, da Brafort10.", "Vocês buscam economia na limpeza?"], "etapa": "atencao"})))
    agente.conversar(db, lead)
    assert len(evo.called("send_text")) == 2 and lead.etapa == Etapa.INTERESSE


def test_pitch_em_sequencia_pode_ser_desligado(db, lead, fake_ia, evo):
    from app.services import config_sistema

    config_sistema.salvar(db, {"pitch_em_sequencia": ""})
    lead.segmento, lead.porte, lead.etapa = "Pousadas", "pequeno", Etapa.INTERESSE
    fake_ia.fila.append(resposta(ferramenta("responder", {"mensagens": ["Temos produtos concentrados."], "etapa": "interesse"})))
    agente.conversar(db, lead)
    sistema = fake_ia.chamadas[0]["system"]
    assert "entregue só a etapa Interesse" in sistema and "PRÓXIMAS" not in sistema
    assert lead.etapa == Etapa.DESEJO


def test_formato_antigo_com_uma_mensagem_ainda_funciona(db, lead, fake_ia, evo):
    lead.segmento, lead.porte = "Pousadas", "pequeno"
    fake_ia.fila.append(resposta(ferramenta("responder", {"mensagem": "Oi!", "etapa": "atencao"})))
    assert agente.conversar(db, lead) == "respondeu"
    assert evo.called("send_text")[-1][3] == "Oi!"
