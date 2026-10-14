"""Roteiros de prospecção por segmento × porte × etapa (importados do .docx da empresa).

Formato esperado do documento:
  Título 2:  "Segmento: Pousadas"
  Título 3:  "1. Pequeno Porte (Foco: Produto Concentrado + Economia)"
  Parágrafos: "Atenção (...): texto", "Interesse: texto", "Desejo: texto", "Ação: texto"
"""

import html
import io
import logging
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ETAPAS, Etapa, Lead, Porte, Roteiro, Segmento
from app.services import config_sistema

logger = logging.getLogger(__name__)

ROTULO_ETAPA = {Etapa.ATENCAO: "Atenção", Etapa.INTERESSE: "Interesse", Etapa.DESEJO: "Desejo", Etapa.ACAO: "Ação"}
ROTULO_PORTE = {Porte.PEQUENO: "Pequeno porte", Porte.MEDIO: "Médio porte", Porte.GRANDE: "Grande porte"}

# Palavras-chave iniciais para os segmentos conhecidos (editáveis no painel).
PALAVRAS_PADRAO = {
    "pousadas": "pousada, hotel, hostel, hospedagem, resort, chalé, flat, albergue, motel, inn",
    "restaurantes": "restaurante, pizzaria, lanchonete, churrascaria, bar, cafeteria, padaria, hamburgueria, sushi, bistrô, cozinha, buffet",
    "lavanderias": "lavanderia, lavagem, tinturaria, lava roupa",
    "supermercados": "supermercado, mercado, mercearia, hortifruti, atacado, atacadista, minimercado, empório",
    "academias": "academia, crossfit, pilates, studio fitness, ginástica, musculação, box",
    "industria alimenticia": "indústria alimentícia, alimentos, frigorífico, laticínio, fábrica de alimentos, panificadora industrial",
    "industria de bebidas": "bebidas, cervejaria, destilaria, engarrafadora, refrigerante, vinícola, água mineral",
    "industria geral": "indústria, fábrica, metalúrgica, manufatura, industrial",
}


class RoteiroError(Exception):
    pass


def normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", sem_acento.lower())).strip()


# --- Importação do .docx --------------------------------------------------------


@dataclass
class SegmentoLido:
    nome: str
    portes: dict[str, dict] = field(default_factory=dict)  # porte -> {"foco": str, "etapas": {etapa: texto}}


def _paragrafos_docx(conteudo: bytes) -> list[tuple[str | None, str]]:
    try:
        xml = zipfile.ZipFile(io.BytesIO(conteudo)).read("word/document.xml").decode("utf-8")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise RoteiroError("O arquivo não é um .docx válido.") from exc
    paragrafos = []
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        estilo = re.search(r'<w:pStyle w:val="([^"]+)"', p)
        texto = html.unescape("".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, re.S))).strip()
        if texto:
            paragrafos.append((estilo.group(1) if estilo else None, texto))
    return paragrafos


def _porte_do_titulo(titulo: str) -> str | None:
    t = normalizar(titulo)
    for porte, chave in ((Porte.PEQUENO, "pequeno"), (Porte.MEDIO, "medio"), (Porte.GRANDE, "grande")):
        if chave in t:
            return porte
    return None


def _etapa_do_paragrafo(texto: str) -> tuple[str, str] | None:
    """ "Interesse: Na Brafort10…" -> ("interesse", "Na Brafort10…") """
    m = re.match(r"^\s*([^:()]+?)\s*(\([^)]*\))?\s*:\s*(.+)$", texto, re.S)
    if not m:
        return None
    rotulo = normalizar(m.group(1))
    for etapa in ETAPAS:
        if rotulo == normalizar(ROTULO_ETAPA[etapa]):
            return etapa, m.group(3).strip()
    return None


def ler_docx(conteudo: bytes) -> list[SegmentoLido]:
    segmentos: list[SegmentoLido] = []
    porte_atual: str | None = None
    for estilo, texto in _paragrafos_docx(conteudo):
        titulo = (estilo or "").lower()
        if titulo.startswith("heading2") or titulo.startswith("ttulo2") or normalizar(texto).startswith("segmento "):
            nome = re.sub(r"^\s*segmento\s*:\s*", "", texto, flags=re.I).strip()
            segmentos.append(SegmentoLido(nome=nome))
            porte_atual = None
        elif titulo.startswith("heading3") or titulo.startswith("ttulo3") or re.match(r"^\s*\d+\.\s", texto):
            porte_atual = _porte_do_titulo(texto)
            if segmentos and porte_atual:
                foco = re.search(r"foco\s*:\s*([^)]+)", texto, re.I)
                segmentos[-1].portes[porte_atual] = {"foco": foco.group(1).strip() if foco else "", "etapas": {}}
        elif segmentos and porte_atual:
            achado = _etapa_do_paragrafo(texto)
            if achado:
                etapa, conteudo_etapa = achado
                segmentos[-1].portes[porte_atual]["etapas"][etapa] = conteudo_etapa
    segmentos = [s for s in segmentos if s.portes]
    if not segmentos:
        raise RoteiroError("Não encontrei roteiros no documento. Use títulos \"Segmento: …\" e \"1. Pequeno Porte (Foco: …)\".")
    return segmentos


def importar(db: Session, segmentos: list[SegmentoLido]) -> dict:
    """Cria ou substitui os roteiros dos segmentos do documento. Mantém as palavras-chave já editadas."""
    criados = atualizados = 0
    for lido in segmentos:
        segmento = db.scalar(select(Segmento).where(Segmento.nome == lido.nome))
        if segmento is None:
            segmento = Segmento(nome=lido.nome, palavras_chave=PALAVRAS_PADRAO.get(normalizar(lido.nome), lido.nome.lower()))
            db.add(segmento)
            criados += 1
        else:
            atualizados += 1
            # Apaga antes de inserir: o banco não aceita dois textos da mesma etapa nem por um instante.
            for antigo in list(segmento.roteiros):
                db.delete(antigo)
            db.flush()
        segmento.roteiros = [
            Roteiro(porte=porte, foco=dados["foco"], etapa=etapa, texto=texto)
            for porte, dados in lido.portes.items()
            for etapa, texto in dados["etapas"].items()
        ]
    db.flush()
    total = sum(len(d["etapas"]) for s in segmentos for d in s.portes.values())
    logger.info("roteiros importados", extra={"segmentos": len(segmentos), "textos": total})
    return {"segmentos": len(segmentos), "novos": criados, "atualizados": atualizados, "textos": total}


# --- Uso pelo agente ----------------------------------------------------------------


def preencher(texto: str, db: Session) -> str:
    """Troca os campos em aberto do documento pelos valores das Configurações."""
    v = config_sistema.valores(db)
    texto = re.sub(r"\[\s*seu nome\s*\]", v["atendente_nome"], texto, flags=re.I)
    texto = re.sub(r"at[eé]\s+X\s*%", v["economia_texto"], texto, flags=re.I)
    return texto


def segmento_por_palavras(db: Session, *textos: str | None) -> Segmento | None:
    """Primeiro segmento cuja palavra-chave aparece na categoria/termo/nome. Termos mais longos ganham."""
    alvo = f" {normalizar(' '.join(t for t in textos if t))} "
    melhor, tamanho = None, 0
    for segmento in db.scalars(select(Segmento).order_by(Segmento.id)):
        for palavra in filter(None, (normalizar(p) for p in segmento.palavras_chave.split(","))):
            if f" {palavra} " in alvo and len(palavra) > tamanho:
                melhor, tamanho = segmento, len(palavra)
    return melhor


def roteiro_do_lead(db: Session, lead: Lead) -> dict | None:
    """{"segmento", "porte", "foco", "etapas": {etapa: texto já preenchido}} ou None se não houver roteiro."""
    if not lead.segmento:
        return None
    segmento = db.scalar(select(Segmento).where(Segmento.nome == lead.segmento))
    if segmento is None:
        return None
    porte = lead.porte or Porte.PEQUENO
    textos = [r for r in segmento.roteiros if r.porte == porte] or [r for r in segmento.roteiros if r.porte == Porte.PEQUENO]
    if not textos:
        return None
    return {
        "segmento": segmento.nome,
        "porte": textos[0].porte,
        "foco": textos[0].foco,
        "etapas": {r.etapa: preencher(r.texto, db) for r in sorted(textos, key=lambda r: ETAPAS.index(r.etapa))},
    }
