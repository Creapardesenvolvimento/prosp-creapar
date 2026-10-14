"""Chamadas de IA pelo formato da API da Anthropic (Messages).

Com IA_BASE_URL preenchida, as chamadas passam por um gateway compatível (ex.: OmniRouter),
que escolhe o provedor e troca de plataforma quando os créditos acabam. Por isso o código usa
só o que qualquer provedor atrás do gateway entende: mensagens, system e tools — nada de betas
ou parâmetros exclusivos da Anthropic.
"""

import json
import logging
import re

import anthropic

from app.config import get_settings
from app.services import config_sistema

logger = logging.getLogger(__name__)


class IAError(Exception):
    pass


def get_client() -> anthropic.Anthropic:
    settings = get_settings()
    chave = settings.ia_api_key or settings.anthropic_api_key
    if not chave:
        raise IAError("Chave de IA não configurada (IA_API_KEY ou ANTHROPIC_API_KEY).")
    return anthropic.Anthropic(api_key=chave, base_url=settings.ia_base_url or None, timeout=120, max_retries=2)


def modelo(db=None) -> str:
    """Modelo escolhido em Configurações; sem banco, o do .env."""
    if db is not None:
        return config_sistema.valores(db)["ia_modelo"]
    return get_settings().ia_modelo


def chamar(**kwargs) -> anthropic.types.Message:
    """messages.create com as mensagens de erro já traduzidas para o painel."""
    try:
        response = get_client().messages.create(**kwargs)
    except anthropic.APIConnectionError as exc:
        raise IAError("Não consegui falar com o serviço de IA. Tente de novo em instantes.") from exc
    except anthropic.AuthenticationError as exc:
        raise IAError("Chave de IA inválida.") from exc
    except anthropic.RateLimitError as exc:
        raise IAError("Limite de uso da IA atingido. Tente de novo em alguns minutos.") from exc
    except anthropic.APIStatusError as exc:
        raise IAError(f"O serviço de IA recusou o pedido ({exc.status_code}): {exc.message}") from exc
    if response.stop_reason == "refusal":
        raise IAError("O modelo recusou o pedido.")
    return response


_FERRAMENTA_VARIACOES = {
    "name": "registrar_variacoes",
    "description": "Registra as variações da mensagem de abertura.",
    "input_schema": {
        "type": "object",
        "properties": {"variacoes": {"type": "array", "items": {"type": "string"}}},
        "required": ["variacoes"],
    },
}

def _sistema_variacoes(db) -> str:
    v = config_sistema.valores(db) if db is not None else config_sistema.padroes()
    return f"""Você escreve a PRIMEIRA mensagem de WhatsApp de uma prospecção B2B: {v['atendente_nome']}, da {v['empresa_nome']}, chamando uma empresa pela primeira vez.

Essa primeira mensagem é só uma saudação curta para abrir a conversa. A apresentação e a oferta vêm depois que a empresa responder,
então NÃO fale de produto, preço, desconto nem da {v['empresa_nome']} além de, no máximo, o nome.

Regras para cada mensagem:
- Português do Brasil, tom humano e cordial, como alguém real escrevendo do celular.
- Bem curta: uma ou duas frases, até cerca de 120 caracteres.
- Use o marcador {{empresa}} para o nome da empresa (e {{cidade}} se fizer sentido). Não use outros marcadores.
- Termine com uma pergunta simples que dê vontade de responder (ex.: confirmar se fala com a empresa ou com o responsável).
- Sem links, no máximo um emoji. Não use "bom dia"/"boa tarde" fixos: a mensagem pode sair em qualquer horário (use "Olá", "Oi", "Tudo bem?").
- As variações precisam ser realmente diferentes entre si, não só sinônimos.

Entregue as variações chamando a ferramenta registrar_variacoes."""


def _variacoes_da_resposta(response) -> list:
    for bloco in response.content:
        if bloco.type == "tool_use" and bloco.name == "registrar_variacoes":
            return (bloco.input or {}).get("variacoes") or []
    # Alguns provedores atrás do gateway respondem em texto; aceita um JSON no meio da resposta.
    texto = "".join(b.text for b in response.content if b.type == "text")
    achado = re.search(r"\{.*\}", texto, re.S)
    if achado:
        try:
            return json.loads(achado.group(0)).get("variacoes") or []
        except (ValueError, AttributeError):
            pass
    raise IAError("Resposta inesperada do modelo.")


def gerar_variacoes(*, nicho: str, oferta: str, exemplos: list[str], quantidade: int = 10, db=None) -> list[str]:
    """Gera variações da mensagem de abertura. Os marcadores {empresa} e {cidade} vêm preservados."""
    pedido = [f"Nicho dos leads: {nicho or 'não informado'}", f"Oferta/promessa da campanha: {oferta or 'não informada'}"]
    if exemplos:
        pedido.append("Mensagens que já existem (não repita, use só como referência de tom):\n" + "\n".join(f"- {e}" for e in exemplos[:10]))
    pedido.append(f"Escreva {quantidade} variações.")
    response = chamar(
        model=modelo(db),
        max_tokens=4000,
        system=_sistema_variacoes(db),
        tools=[_FERRAMENTA_VARIACOES],
        messages=[{"role": "user", "content": "\n\n".join(pedido)}],
    )
    variacoes = _variacoes_da_resposta(response)
    logger.info("variações geradas pela IA", extra={"quantidade": len(variacoes), "modelo": response.model})
    return [v.strip() for v in variacoes if isinstance(v, str) and v.strip()]
