"""Orquestra o resumo de notícias por período: calcula a janela de data a
partir do número de dias escolhido na UI, busca os artigos PROCESSED da
janela (reaproveitando o filtro de data da busca), concatena título+resumo
já existentes num único prompt e pede à cascata de LLM uma síntese em texto
corrido — sem embedding/KNN, sem map-reduce, sem clustering por tópico (ver
docs/poc-resumo-por-periodo.md, escopo do POC).
"""

import logging
import time
from datetime import date, datetime, timedelta

import litellm
from sqlmodel import Session

import pipeline_state
from llm_cascade import LLM_CASCADE
from models import Article
from search.repository import get_candidate_article_ids
from summary.config import SUMMARY_LLM_TIMEOUT_SECONDS, SUMMARY_MAX_ARTICLES
from summary.repository import get_articles_for_summary
from summary.schemas import SummaryResponse, SummarySourceItem
from timezone_utils import DISPLAY_TIMEZONE, as_aware_utc, local_date_to_utc_naive

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Você é um editor de notícias neutro e imparcial do agregador Dr. Occam. "
    "Você recebe uma lista numerada de notícias já resumidas (título e "
    "resumo neutros) publicadas num mesmo período. Produza um texto corrido "
    "em português do Brasil que sintetize os principais assuntos do "
    "período, sem ser uma lista mecânica de manchetes coladas: agrupe "
    "naturalmente notícias sobre o mesmo fato ou tema relacionado, evitando "
    "repetição. Seja estritamente factual, sem adjetivos sensacionalistas, "
    "sem opinião e sem viés político ou ideológico. NÃO invente informações "
    "que não estejam nos resumos fornecidos. NÃO inclua links, URLs ou "
    "numeração de fonte no texto — a lista de fontes é exibida "
    "separadamente pelo sistema. Responda apenas com o texto corrido da "
    "síntese, sem markdown, sem títulos de seção, sem frases de abertura "
    'como "Aqui está um resumo:".'
)


class SummaryUnavailableError(RuntimeError):
    """Levantada quando a cascata de LLM falha ou estoura o timeout ao
    gerar a síntese. Ao contrário da extração de intenção de data da busca,
    aqui não há um "modo sem IA" razoável — o texto de síntese É o produto
    —, então o router traduz isso num 503 explícito."""


def _window_for_days(days: int, today: date) -> tuple[date, date]:
    return today - timedelta(days=days - 1), today


def _build_prompt(articles: list[Article]) -> str:
    lines = [
        f"{i}. [{article.ai_title or '(sem título)'}] {article.ai_summary or ''}"
        for i, article in enumerate(articles, start=1)
    ]
    return "\n".join(lines)


def _build_source_item(article: Article) -> SummarySourceItem:
    return SummarySourceItem(
        title=article.ai_title or "(sem título)",
        url=article.original_url,
        published_at=as_aware_utc(article.created_at),
    )


async def build_period_summary(session: Session, days: int) -> SummaryResponse:
    today = datetime.now(DISPLAY_TIMEZONE).date()
    start_date, end_date = _window_for_days(days, today)

    start_at = local_date_to_utc_naive(start_date, end_of_day=False)
    end_at = local_date_to_utc_naive(end_date, end_of_day=True)

    candidate_ids = get_candidate_article_ids(session, start_at, end_at)
    if not candidate_ids:
        return SummaryResponse(
            window_days=days,
            start_date=start_date,
            end_date=end_date,
            article_count=0,
            truncated=False,
            summary_text="",
            sources=[],
            pipeline_running=pipeline_state.is_running(),
        )

    articles, truncated = get_articles_for_summary(session, candidate_ids, SUMMARY_MAX_ARTICLES)

    if not LLM_CASCADE:
        raise SummaryUnavailableError("Nenhum modelo de IA configurado")

    primary, *fallbacks = LLM_CASCADE
    primary_kwargs = {k: v for k, v in primary.items() if k != "model"}
    prompt = _build_prompt(articles)

    started_at = time.monotonic()
    try:
        response = await litellm.acompletion(
            model=primary["model"],
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            timeout=SUMMARY_LLM_TIMEOUT_SECONDS,
            fallbacks=fallbacks or None,
            **primary_kwargs,
        )
    except Exception as exc:
        elapsed = time.monotonic() - started_at
        logger.exception(
            "Resumo por período: falha na cascata de LLM (days=%d artigos=%d tempo=%.1fs)",
            days, len(articles), elapsed,
        )
        raise SummaryUnavailableError(
            "Não foi possível gerar o resumo no momento"
        ) from exc
    elapsed = time.monotonic() - started_at

    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    logger.info(
        "Resumo por período: days=%d artigos=%d truncated=%s prompt_tokens=%d "
        "completion_tokens=%d tempo=%.1fs",
        days, len(articles), truncated, prompt_tokens, completion_tokens, elapsed,
    )

    summary_text = (response.choices[0].message.content or "").strip()

    return SummaryResponse(
        window_days=days,
        start_date=start_date,
        end_date=end_date,
        article_count=len(articles),
        truncated=truncated,
        summary_text=summary_text,
        sources=[_build_source_item(article) for article in articles],
        pipeline_running=pipeline_state.is_running(),
    )
