"""Ajudantes para simular o modelo de IA e montar roteiros nos testes do agente."""

import io
import zipfile
from types import SimpleNamespace
from xml.sax.saxutils import escape

DOCX_EXEMPLO = [
    ("Heading2", "Segmento: Pousadas"),
    ("Heading3", "1. Pequeno Porte (Foco: Produto Concentrado + Economia)"),
    (None, "Atenção (Resposta ao Olá/Bom dia): Olá! Meu nome é [Seu Nome] e sou da Brafort10. Vi que sua pousada busca economia."),
    (None, "Interesse: Produtos super concentrados que rendem mais."),
    (None, "Desejo: Reduza seus gastos em até X% com ambientes impecáveis."),
    (None, "Ação: Que tal uma análise gratuita?"),
    ("Heading3", "3. Grande Porte (Foco: Treinamento)"),
    (None, "Atenção (Resposta ao Olá/Bom dia): Olá! Sou [Seu Nome], da Brafort10. Pousadas grandes buscam padronização."),
    (None, "Interesse: Treinamento certificado para a equipe."),
    (None, "Desejo: Equipe capacitada e sem desperdício."),
    (None, "Ação: Podemos agendar uma apresentação do treinamento?"),
    ("Heading2", "Segmento: Lavanderias"),
    ("Heading3", "1. Pequeno Porte (Foco: Economia)"),
    (None, "Atenção: Olá, aqui é [Seu Nome]."),
    (None, "Ação: Vamos conversar?"),
]


def montar_docx(paragrafos=DOCX_EXEMPLO) -> bytes:
    corpo = []
    for estilo, texto in paragrafos:
        ppr = f'<w:pPr><w:pStyle w:val="{estilo}"/></w:pPr>' if estilo else ""
        corpo.append(f"<w:p>{ppr}<w:r><w:t xml:space=\"preserve\">{escape(texto)}</w:t></w:r></w:p>")
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        + "".join(corpo)
        + "</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("word/document.xml", xml)
    return buffer.getvalue()


def texto(t):
    return SimpleNamespace(type="text", text=t)


def ferramenta(nome, entrada, id_=None):
    return SimpleNamespace(type="tool_use", name=nome, input=entrada, id=id_ or f"tu_{nome}")


def resposta(*blocos, stop_reason="end_turn"):
    return SimpleNamespace(content=list(blocos), stop_reason=stop_reason, model="modelo-de-teste")


class FakeIA:
    """Devolve as respostas na ordem; uma função recebe os kwargs da chamada e devolve a resposta."""

    def __init__(self):
        self.fila = []
        self.chamadas = []
        self.erro = None
        self.messages = self

    def create(self, **kwargs):
        self.chamadas.append(kwargs)
        if self.erro:
            raise self.erro
        if not self.fila:
            raise AssertionError("chamada inesperada à IA")
        proxima = self.fila.pop(0)
        return proxima(kwargs) if callable(proxima) else proxima
