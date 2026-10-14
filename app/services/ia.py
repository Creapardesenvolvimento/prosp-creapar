"""Chamadas ao Claude (Anthropic SDK)."""

import json
import logging

import anthropic

from app.config import get_settings

logger = logging.getLogger(__name__)


class IAError(Exception):
    pass


def get_client() -> anthropic.Anthropic:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise IAError("ANTHROPIC_API_KEY não configurada.")
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


_SCHEMA_VARIACOES = {
    "type": "object",
    "properties": {"variacoes": {"type": "array", "items": {"type": "string"}}},
    "required": ["variacoes"],
    "additionalProperties": False,
}

_SISTEMA_VARIACOES = """Você escreve a primeira mensagem de WhatsApp de uma prospecção B2B da Creapar, \
agência de marketing digital de São Paulo, para pequenas e médias empresas.

Regras para cada mensagem:
- Português do Brasil, tom humano e direto, como alguém real escrevendo do celular. Nada de linguagem de anúncio.
- Curta: no máximo 3 frases e cerca de 300 caracteres.
- Use o marcador {empresa} para o nome da empresa e, se fizer sentido, {cidade} para a cidade. Não use outros marcadores.
- Termine com uma única pergunta simples que convide a responder.
- Sem links, sem emojis em excesso (no máximo um), sem preços, sem prometer resultados garantidos.
- As variações precisam ser realmente diferentes entre si na estrutura e nas palavras, não só sinônimos."""


def gerar_variacoes(*, nicho: str, oferta: str, exemplos: list[str], quantidade: int = 10) -> list[str]:
    """Gera variações da mensagem de abertura. Os marcadores {empresa} e {cidade} vêm preservados."""
    settings = get_settings()
    pedido = [f"Nicho dos leads: {nicho or 'não informado'}", f"Oferta/promessa da campanha: {oferta or 'não informada'}"]
    if exemplos:
        pedido.append("Mensagens que já existem (não repita, use só como referência de tom):\n" + "\n".join(f"- {e}" for e in exemplos[:10]))
    pedido.append(f"Escreva {quantidade} variações.")
    try:
        response = get_client().beta.messages.create(
            model=settings.ia_modelo,
            max_tokens=4000,
            system=_SISTEMA_VARIACOES,
            messages=[{"role": "user", "content": "\n\n".join(pedido)}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA_VARIACOES}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIConnectionError as exc:
        raise IAError("Não consegui falar com a Anthropic. Tente de novo em instantes.") from exc
    except anthropic.AuthenticationError as exc:
        raise IAError("ANTHROPIC_API_KEY inválida.") from exc
    except anthropic.RateLimitError as exc:
        raise IAError("Limite de uso da Anthropic atingido. Tente de novo em alguns minutos.") from exc
    except anthropic.APIStatusError as exc:
        raise IAError(f"A Anthropic recusou o pedido ({exc.status_code}): {exc.message}") from exc

    if response.stop_reason == "refusal":
        raise IAError("O modelo recusou gerar as variações.")
    texto = next((b.text for b in response.content if b.type == "text"), "")
    try:
        variacoes = json.loads(texto)["variacoes"]
    except (ValueError, KeyError, TypeError) as exc:
        raise IAError("Resposta inesperada do modelo.") from exc
    logger.info("variações geradas pela IA", extra={"quantidade": len(variacoes), "modelo": response.model})
    return [v.strip() for v in variacoes if isinstance(v, str) and v.strip()]
