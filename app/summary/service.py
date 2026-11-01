"""Orquestra o resumo de notícias por período: calcula a janela de data a
partir do número de dias escolhido na UI, busca os artigos PROCESSED da
janela (reaproveitando o filtro de data da busca), concatena título+resumo
já existentes num único prompt e pede à cascata de LLM uma síntese em texto
corrido — sem embedding/KNN, sem map-reduce, sem clustering por tópico (ver
docs/poc-resumo-por-periodo.md, escopo do POC). A chamada ao LLM em si
fica no motor compartilhado (summary/engine.py).
"""

from datetime import date, datetime, timedelta

from sqlmodel import Session

import pipeline_state
from models import Article
from search.repository import get_candidate_article_ids
from summary.config import SUMMARY_MAX_ARTICLES
from summary.engine import synthesize
from summary.repository import get_articles_for_summary
from summary.schemas import SummaryResponse, SummarySourceItem
from timezone_utils import DISPLAY_TIMEZONE, as_aware_utc, local_date_to_utc_naive

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

    result = await synthesize(
        _SYSTEM_PROMPT,
        _build_prompt(articles),
        f"período days={days} artigos={len(articles)} truncated={truncated}",
    )
    summary_text = result.text

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
