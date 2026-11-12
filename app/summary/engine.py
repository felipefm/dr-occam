"""Motor de síntese do resumo por IA da pauta: recebe um prompt de sistema
e o texto com as notícias, chama a cascata de LLM com o timeout próprio do
resumo e registra tokens e tempo no log. (Nasceu da POC de resumo por
período, abandonada — ver docs/poc-resumo-por-periodo.md.)

Só um resumo roda por vez no processo: o prompt de um resumo é ordens de
magnitude maior que os prompts por artigo do pipeline, e o LLM local é
compartilhado com o pipeline e com os embeddings. Dois cliques seguidos
gerando dois prompts grandes em paralelo é o tipo de carga que já derrubou
o host uma vez (ver pipeline_state.py). Quem chega com outro resumo em
andamento recebe `SummaryBusyError` na hora, em vez de entrar numa fila
que pode levar minutos.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import litellm

from llm_cascade import get_llm_cascade
from summary.config import SUMMARY_LLM_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()


class SummaryUnavailableError(RuntimeError):
    """A cascata de LLM falhou, estourou o timeout ou não há modelo
    configurado. Não há um "modo sem IA" razoável — o texto É o produto —,
    então os routers traduzem isso num 503."""


class SummaryBusyError(RuntimeError):
    """Já há um resumo sendo gerado. Os routers traduzem isso num 409."""


def is_busy() -> bool:
    return _lock.locked()


@dataclass
class SynthesisResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    elapsed_seconds: float


@asynccontextmanager
async def exclusive() -> AsyncIterator[None]:
    """Reserva o motor para uma geração inteira (que pode ter mais de uma
    chamada ao LLM, como a rodada de complemento da cobertura). Levanta
    SummaryBusyError na hora se outro resumo estiver em andamento."""
    if not get_llm_cascade():
        raise SummaryUnavailableError("Nenhum modelo de IA configurado")
    if _lock.locked():
        raise SummaryBusyError(
            "Já há um resumo sendo gerado. Espere ele terminar e tente de novo."
        )
    async with _lock:
        yield


async def complete(system_prompt: str, user_prompt: str, label: str) -> SynthesisResult:
    """Uma chamada à cascata de LLM. Chamar dentro de `exclusive()`.
    `label` identifica a origem no log (ex.: "pauta atual formato=briefing
    artigos=8")."""
    if not _lock.locked():
        raise RuntimeError("summary.engine.complete() fora de exclusive()")
    primary, *fallbacks = get_llm_cascade()
    primary_kwargs = {k: v for k, v in primary.items() if k != "model"}

    started_at = time.monotonic()
    try:
        response = await litellm.acompletion(
            model=primary["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            timeout=SUMMARY_LLM_TIMEOUT_SECONDS,
            fallbacks=fallbacks or None,
            **primary_kwargs,
        )
    except Exception as exc:
        logger.exception(
            "Resumo (%s): falha na cascata de LLM (tempo=%.1fs)",
            label, time.monotonic() - started_at,
        )
        raise SummaryUnavailableError("Não foi possível gerar o resumo no momento") from exc
    elapsed = time.monotonic() - started_at

    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    logger.info(
        "Resumo (%s): prompt_tokens=%d completion_tokens=%d tempo=%.1fs",
        label, prompt_tokens, completion_tokens, elapsed,
    )

    text = (response.choices[0].message.content or "").strip()
    if not text:
        raise SummaryUnavailableError("A IA devolveu um resumo vazio")
    return SynthesisResult(text, prompt_tokens, completion_tokens, elapsed)
