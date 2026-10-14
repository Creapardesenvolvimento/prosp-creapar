from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.services import config_sistema, ia
from app.services.ia import IAError


def bloco(**campos):
    return SimpleNamespace(**campos)


def resposta(*blocos, stop_reason="end_turn"):
    return SimpleNamespace(content=list(blocos), stop_reason=stop_reason, model="modelo-servido")


@pytest.fixture
def chamadas(monkeypatch):
    registro = {"kwargs": None, "resposta": None, "erro": None}

    class Mensagens:
        def create(self, **kwargs):
            registro["kwargs"] = kwargs
            if registro["erro"]:
                raise registro["erro"]
            return registro["resposta"]

    monkeypatch.setattr(ia, "get_client", lambda: SimpleNamespace(messages=Mensagens()))
    return registro


def test_variacoes_pela_ferramenta(chamadas, db):
    config_sistema.salvar(db, {"ia_modelo": "cc/claude-sonnet-5"})
    chamadas["resposta"] = resposta(
        bloco(type="text", text="Aqui estão:"),
        bloco(type="tool_use", name="registrar_variacoes", input={"variacoes": [" Oi {empresa}! ", "", "Olá {empresa}"]}),
    )
    assert ia.gerar_variacoes(nicho="padarias", oferta="mais pedidos", exemplos=["Bom dia {empresa}"], db=db) == [
        "Oi {empresa}!",
        "Olá {empresa}",
    ]
    kwargs = chamadas["kwargs"]
    assert kwargs["model"] == "cc/claude-sonnet-5"  # o modelo vem das Configurações
    assert kwargs["tools"][0]["name"] == "registrar_variacoes"
    # nada exclusivo da Anthropic: o gateway pode mandar para outro provedor
    assert not {"betas", "fallbacks", "output_config"} & kwargs.keys()
    assert "Bom dia {empresa}" in kwargs["messages"][0]["content"]


def test_variacoes_em_texto_quando_o_provedor_nao_usa_a_ferramenta(chamadas):
    chamadas["resposta"] = resposta(bloco(type="text", text='Claro! {"variacoes": ["Oi {empresa}", "Olá {empresa}"]} Espero que ajude.'))
    assert ia.gerar_variacoes(nicho="", oferta="", exemplos=[]) == ["Oi {empresa}", "Olá {empresa}"]


def test_resposta_sem_variacoes(chamadas):
    chamadas["resposta"] = resposta(bloco(type="text", text="Não entendi."))
    with pytest.raises(IAError, match="inesperada"):
        ia.gerar_variacoes(nicho="", oferta="", exemplos=[])


def test_recusa(chamadas):
    chamadas["resposta"] = resposta(stop_reason="refusal")
    with pytest.raises(IAError, match="recusou"):
        ia.gerar_variacoes(nicho="", oferta="", exemplos=[])


def test_erros_viram_mensagens_para_o_painel(chamadas):
    request = httpx.Request("POST", "https://router.test/v1/messages")
    chamadas["erro"] = anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=request), body=None)
    with pytest.raises(IAError, match="Chave de IA inválida"):
        ia.gerar_variacoes(nicho="", oferta="", exemplos=[])
    chamadas["erro"] = anthropic.APIConnectionError(request=request)
    with pytest.raises(IAError, match="Não consegui falar"):
        ia.gerar_variacoes(nicho="", oferta="", exemplos=[])


def test_cliente_aponta_para_o_gateway(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "ia_base_url", "https://router.test")
    monkeypatch.setattr(settings, "ia_api_key", "chave-do-router")
    client = ia.get_client()
    assert str(client.base_url).startswith("https://router.test")
    assert client.api_key == "chave-do-router"

    monkeypatch.setattr(settings, "ia_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    with pytest.raises(IAError, match="não configurada"):
        ia.get_client()


def test_modelo_invalido_nas_configuracoes(db):
    with pytest.raises(config_sistema.ConfigError):
        config_sistema.salvar(db, {"ia_modelo": "nome com espaço"})
