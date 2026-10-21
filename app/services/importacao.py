"""Importação de listas de contatos (Excel, CSV, Word ou texto colado) para prospecção.

A lista vira uma "busca" de origem importação: cada contato é guardado como empresa e passa pelo
mesmo processamento das buscas do Apify (blacklist, já era lead, conferência de WhatsApp). Assim os
leads da lista aparecem nas campanhas como os de qualquer busca.

As funções alteram a sessão mas não fazem commit.
"""

import csv
import io
import logging
import re
import unicodedata
import zipfile
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import Busca, BuscaStatus, Campanha, CampanhaStatus, Empresa, User
from app.services.telefone import eh_celular, normalizar_telefone

logger = logging.getLogger(__name__)

TAMANHO_MAXIMO = 10 * 1024 * 1024  # 10 MB
MAXIMO_CONTATOS = 20000
EXTENSOES = (".xlsx", ".xls", ".csv", ".txt", ".docx")

# Sequência que pode ser um telefone: começa e termina com dígito, com espaços, pontos, traços e parênteses no meio.
_TELEFONE = re.compile(r"\+?\(?\d[\d\s().\-]{7,18}\d")
_SEPARADORES = " \t-–—:;,|/•·"

# Palavras dos cabeçalhos, sem acento e em minúsculas.
COLUNAS = {
    "telefone": ("telefone", "fone", "celular", "whatsapp", "whats", "zap", "numero", "contato", "tel", "phone", "mobile"),
    "nome": ("nome", "empresa", "razao", "fantasia", "cliente", "estabelecimento", "loja", "title", "name", "company"),
    "cidade": ("cidade", "municipio", "city"),
    "categoria": ("segmento", "categoria", "ramo", "atividade", "nicho", "category"),
}


class ImportacaoError(Exception):
    pass


@dataclass
class Contato:
    linha: int
    telefone_original: str
    nome: str = ""
    cidade: str = ""
    categoria: str = ""


def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower().strip()


def _celula(valor) -> str:
    """O Excel guarda telefone como número (11999998888.0); devolve sempre texto."""
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor).strip()


# --- Leitura dos arquivos: tudo vira uma lista de linhas (listas de células) -------------


def _decodificar(conteudo: bytes) -> str:
    for codificacao in ("utf-8-sig", "cp1252"):
        try:
            return conteudo.decode(codificacao)
        except UnicodeDecodeError:
            continue
    return conteudo.decode("latin-1")


def _ler_csv(conteudo: bytes) -> list[list[str]]:
    texto = _decodificar(conteudo)
    try:
        dialeto = csv.Sniffer().sniff(texto[:5000], delimiters=";,\t|")
    except csv.Error:
        return [[linha] for linha in texto.splitlines()]  # sem separador: uma coluna só
    return [[_celula(c) for c in linha] for linha in csv.reader(io.StringIO(texto), dialeto)]


def _ler_xlsx(conteudo: bytes) -> list[list[str]]:
    import openpyxl

    try:
        livro = openpyxl.load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise ImportacaoError("Não consegui abrir a planilha .xlsx. Confira se o arquivo não está corrompido.") from exc
    linhas = []
    for aba in livro.worksheets:
        linhas.extend([_celula(c) for c in linha] for linha in aba.iter_rows(values_only=True))
    livro.close()
    return linhas


def _ler_xls(conteudo: bytes) -> list[list[str]]:
    import xlrd

    try:
        livro = xlrd.open_workbook(file_contents=conteudo)
    except xlrd.XLRDError as exc:
        raise ImportacaoError("Não consegui abrir a planilha .xls. Tente salvar como .xlsx ou .csv.") from exc
    linhas = []
    for aba in livro.sheets():
        linhas.extend([_celula(c) for c in aba.row_values(i)] for i in range(aba.nrows))
    return linhas


def _ler_docx(conteudo: bytes) -> list[list[str]]:
    import docx

    try:
        documento = docx.Document(io.BytesIO(conteudo))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise ImportacaoError("Não consegui abrir o documento .docx.") from exc
    linhas = [[p.text] for p in documento.paragraphs if p.text.strip()]
    for tabela in documento.tables:
        linhas.extend([_celula(c.text) for c in linha.cells] for linha in tabela.rows)
    return linhas


def ler_arquivo(nome: str, conteudo: bytes) -> list[list[str]]:
    nome = (nome or "").lower()
    if len(conteudo) > TAMANHO_MAXIMO:
        raise ImportacaoError("O arquivo passou de 10 MB. Divida a lista em partes menores.")
    if nome.endswith(".doc"):
        raise ImportacaoError("Arquivo .doc antigo não é aceito: abra no Word e salve como .docx, ou cole os números no campo de texto.")
    if nome.endswith(".xlsx"):
        return _ler_xlsx(conteudo)
    if nome.endswith(".xls"):
        return _ler_xls(conteudo)
    if nome.endswith(".docx"):
        return _ler_docx(conteudo)
    if nome.endswith((".csv", ".txt")):
        return _ler_csv(conteudo)
    raise ImportacaoError("Formato não aceito. Use Excel (.xlsx ou .xls), CSV, TXT ou Word (.docx).")


def ler_texto(texto: str) -> list[list[str]]:
    """Texto colado: uma linha por contato, com o telefone e, se tiver, o nome."""
    linhas = (texto or "").splitlines()
    if any("\t" in linha for linha in linhas):  # colado do Excel: as colunas vêm separadas por tab
        return [[c.strip() for c in linha.split("\t")] for linha in linhas]
    return [[linha] for linha in linhas]


# --- Extração dos contatos -----------------------------------------------------


def _mapear_cabecalho(linha: list[str]) -> dict[str, int] | None:
    """Se a linha é um cabeçalho com uma coluna de telefone, devolve campo -> índice da coluna."""
    if sum(1 for celula in linha if celula.strip()) < 2:
        return None  # numa coluna só, "linha sem número" não é cabeçalho
    mapa: dict[str, int] = {}
    for indice, celula in enumerate(linha):
        texto = _sem_acento(celula)
        if not texto:
            continue
        # Vale a primeira palavra conhecida: "Nome do contato" é nome, "Telefone da empresa" é telefone.
        for palavra in re.split(r"[^a-z0-9]+", texto):
            campo = next((c for c, chaves in COLUNAS.items() if palavra in chaves), None)
            if campo:
                mapa.setdefault(campo, indice)
                break
    return mapa if "telefone" in mapa else None


def telefones_no_texto(texto: str) -> list[str]:
    return [m.group(0).strip() for m in _TELEFONE.finditer(texto or "") if len(re.sub(r"\D", "", m.group(0))) >= 10]


def _nome_da_linha(celulas: list[str], telefones: list[str]) -> str:
    """O primeiro texto com letras que não é telefone (numa linha de uma célula, o que sobra sem o telefone)."""
    for celula in celulas:
        resto = celula
        for telefone in telefones:
            resto = resto.replace(telefone, " ")
        resto = re.sub(r"\s{2,}", " ", resto).strip(_SEPARADORES + " ")
        if re.search(r"[A-Za-zÀ-ÿ]", resto):
            return resto[:300]
    return ""


def extrair_contatos(linhas: list[list[str]]) -> list[Contato]:
    """Acha os telefones nas linhas. Com cabeçalho, usa as colunas; sem, procura em cada célula."""
    linhas = [[_celula(c) for c in linha] for linha in linhas]
    mapa, inicio = None, 0
    for i, linha in enumerate(linhas[:10]):
        if any(telefones_no_texto(celula) for celula in linha):
            break  # o cabeçalho vem antes do primeiro telefone
        mapa = _mapear_cabecalho(linha)
        if mapa:
            inicio = i + 1
            break

    contatos: list[Contato] = []
    for numero_linha, linha in enumerate(linhas[inicio:], start=inicio + 1):
        if not any(linha):
            continue
        if mapa:
            pegar = lambda campo: linha[mapa[campo]] if campo in mapa and mapa[campo] < len(linha) else ""  # noqa: E731
            celula_telefone = pegar("telefone")
            telefones = telefones_no_texto(celula_telefone) or ([celula_telefone] if celula_telefone else [])
            nome = pegar("nome")[:300]
            cidade, categoria = pegar("cidade")[:200], pegar("categoria")[:200]
        else:
            telefones = [t for celula in linha for t in telefones_no_texto(celula)]
            nome = _nome_da_linha(linha, telefones)
            cidade = categoria = ""
        for telefone in telefones:
            contatos.append(Contato(numero_linha, telefone[:50], nome, cidade, categoria))
    return contatos


# --- Gravação -----------------------------------------------------------------------


def importar(
    db: Session,
    contatos: list[Contato],
    *,
    nome_lista: str,
    cidade: str = "",
    segmento: str = "",
    campanha: Campanha | None = None,
    user: User | None = None,
) -> Busca:
    """Cria a lista (busca de origem importação) com os contatos guardados, pronta para processar."""
    nome_lista = nome_lista.strip()[:200]
    if not nome_lista:
        raise ImportacaoError("Dê um nome para a lista.")
    if not contatos:
        raise ImportacaoError("Não achei nenhum telefone. Confira se a lista tem uma coluna de telefone ou um número por linha.")
    if len(contatos) > MAXIMO_CONTATOS:
        raise ImportacaoError(f"A lista tem {len(contatos)} telefones; o máximo por importação é {MAXIMO_CONTATOS}. Divida em partes.")
    if campanha is not None and campanha.status == CampanhaStatus.ENCERRADA:
        raise ImportacaoError("A campanha escolhida está encerrada.")

    busca = Busca(
        termo=nome_lista,
        cidade=cidade.strip()[:200],
        limite=len(contatos),
        origem="importacao",
        # "executando" é o estado de quem espera o processamento; sem run do Apify, o polling não a toca.
        status=BuscaStatus.EXECUTANDO,
        campanha_id=campanha.id if campanha else None,
        criado_por_id=user.id if user else None,
    )
    db.add(busca)
    db.flush()
    for contato in contatos:
        telefone = normalizar_telefone(contato.telefone_original)
        db.add(
            Empresa(
                busca_id=busca.id,
                nome=contato.nome,
                telefone_original=contato.telefone_original,
                telefone=telefone,
                celular=eh_celular(telefone),
                categoria=(contato.categoria or segmento.strip())[:200] or None,
                cidade=(contato.cidade or cidade.strip())[:200] or None,
                dados={"origem": "importacao", "linha": contato.linha},
            )
        )
    db.flush()
    logger.info("lista importada", extra={"busca_id": busca.id, "contatos": len(contatos), "campanha_id": busca.campanha_id})
    return busca
