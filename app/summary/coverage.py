"""Conferência de cobertura do resumo da pauta: quais notícias o texto
gerado deixou de citar, e a frase "Também na pauta: …" que completa o
resumo com elas.

A detecção não usa o modelo de texto (que é justamente quem se perde com
pautas grandes): compara, por embedding, cada notícia com cada frase do
resumo e fica com a frase mais parecida.

- semelhança >= COVERAGE_HIGH            → citada
- semelhança <  COVERAGE_LOW             → faltando
- no meio (zona de dúvida)               → citada só se a frase mais
  parecida também tiver ao menos metade dos nomes próprios do título da
  notícia; senão (ou se o título não tiver nome próprio), faltando.

Na dúvida, a notícia é tratada como faltando: o custo de errar para esse
lado é uma notícia citada duas vezes; o do outro lado é uma notícia
sumida do resumo, que é o problema que isto existe para resolver.

Calibração (2026-11-01, nomic-embed-text-v1.5): numa pauta de 24 notícias
com cobertura conferida à mão, as citadas ficaram entre 0,775 e 0,907 e as
que faltaram entre 0,692 e 0,755 — ver docs/resumo-da-pauta.md.
"""

import logging
import math
import os
import re
from dataclasses import dataclass

from embeddings.config import EMBEDDING_DOCUMENT_PREFIX, EMBEDDING_QUERY_PREFIX
from embeddings.service import generate_embeddings

logger = logging.getLogger(__name__)

COVERAGE_HIGH: float = float(os.getenv("PAUTA_COVERAGE_HIGH", "0.80"))
COVERAGE_LOW: float = float(os.getenv("PAUTA_COVERAGE_LOW", "0.74"))

ALSO_PREFIX = "Também na pauta:"

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+|;\s*")
_WORD = re.compile(r"[\wÀ-ÿ][\wÀ-ÿ\-]*")
# Palavras que aparecem com inicial maiúscula sem serem nomes próprios.
_NOT_NAMES = {
    "a", "o", "as", "os", "um", "uma", "de", "da", "do", "das", "dos", "e", "em", "no", "na",
    "nos", "nas", "por", "para", "com", "sem", "sobre", "após", "entre", "ao", "aos", "à",
    "que", "se", "seu", "sua", "mais", "novo", "nova", "novas", "novos",
}


@dataclass
class CoverageItem:
    title: str
    summary: str


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if len(s.strip()) >= 15]


def _proper_names(title: str, summary: str) -> set[str]:
    """Nomes próprios do título: palavras do título que aparecem com
    inicial maiúscula no MEIO de uma frase do resumo da notícia (onde a
    maiúscula não é só por ser começo de frase ou título em Title Case),
    mais siglas (ANAC, IBGE, EUA)."""
    mid_sentence_caps = set()
    for sentence in re.split(r"(?<=[.!?:])\s+", summary):
        words = _WORD.findall(sentence)
        mid_sentence_caps.update(w for w in words[1:] if w[0].isupper())
    names = set()
    for word in _WORD.findall(title):
        if word.lower() in _NOT_NAMES or len(word) < 3:
            continue
        if word.isupper() or word in mid_sentence_caps:
            names.add(word)
    return names


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _names_in(sentence: str, names: set[str]) -> int:
    return sum(1 for n in names if re.search(rf"(?<![\wÀ-ÿ]){re.escape(n)}(?![\wÀ-ÿ])", sentence))


async def find_missing(text: str, items: list[CoverageItem]) -> list[int]:
    """Índices (0-based) das notícias que o texto não cita. Levanta exceção
    se o provedor de embeddings falhar — o chamador decide o que fazer."""
    sentences = split_sentences(text)
    if not sentences:
        return list(range(len(items)))

    vectors = await generate_embeddings(
        [f"{EMBEDDING_DOCUMENT_PREFIX}{s}" for s in sentences]
        + [f"{EMBEDDING_QUERY_PREFIX}{it.title}. {it.summary[:400]}" for it in items]
    )
    sentence_vecs, item_vecs = vectors[: len(sentences)], vectors[len(sentences) :]

    missing = []
    for i, (item, vec) in enumerate(zip(items, item_vecs)):
        scores = [_cosine(vec, s) for s in sentence_vecs]
        best = max(range(len(scores)), key=scores.__getitem__)
        score = scores[best]
        if score >= COVERAGE_HIGH:
            verdict = "citada"
        elif score < COVERAGE_LOW:
            verdict = "faltando"
        else:
            names = _proper_names(item.title, item.summary)
            found = _names_in(sentences[best], names)
            verdict = "citada" if names and found >= math.ceil(len(names) / 2) else "faltando"
        logger.info(
            "Cobertura: notícia %d sim=%.3f %s — %s", i + 1, score, verdict, item.title[:70]
        )
        if verdict == "faltando":
            missing.append(i)
    return missing


COMPLETION_PROMPT = (
    "Você recebe notícias numeradas que ficaram de fora de um resumo. Para "
    "cada uma, escreva um trecho curto, de no máximo 15 palavras, com o fato "
    "principal, em português do Brasil, de forma estritamente factual e sem "
    "opinião. Comece cada trecho com letra minúscula, a não ser que comece "
    "com um nome próprio ou sigla. Responda com uma linha por notícia, no "
    "formato \"N: trecho\", na mesma numeração recebida, e nada mais — sem "
    "markdown, sem introdução, sem links."
)


def completion_input(items: list[CoverageItem]) -> str:
    return "\n\n".join(
        f"{n}: {it.title}\n{it.summary[:600]}" for n, it in enumerate(items, start=1)
    )


_LINE = re.compile(r"^\s*(\d{1,3})\s*[:.)\-–]\s*(.+?)\s*$")


def parse_completion(answer: str, items: list[CoverageItem]) -> list[str]:
    """Um trecho por notícia, na ordem de `items`. Notícia sem linha
    válida na resposta entra pelo título — a cobertura é garantida pelo
    código, não pela obediência do modelo."""
    by_number: dict[int, str] = {}
    for line in answer.splitlines():
        match = _LINE.match(line)
        if match:
            by_number.setdefault(int(match.group(1)), match.group(2))
    fragments = []
    for n, item in enumerate(items, start=1):
        fragment = by_number.get(n) or item.title
        fragments.append(fragment.strip().rstrip(".;").strip())
    return fragments


def append_also(text: str, fragments: list[str]) -> str:
    """Acrescenta os trechos ao "Também na pauta:" do fim do texto, se o
    último parágrafo já for um; senão, cria esse parágrafo."""
    joined = "; ".join(fragments)
    paragraphs = text.rstrip().split("\n")
    last = paragraphs[-1].strip()
    if last.startswith(ALSO_PREFIX):
        paragraphs[-1] = f"{last.rstrip('.').rstrip()}; {joined}."
        return "\n".join(paragraphs)
    return f"{text.rstrip()}\n\n{ALSO_PREFIX} {joined}."
