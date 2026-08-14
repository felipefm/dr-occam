"""Schemas Pydantic do resumo de notícias por período."""

from datetime import date, datetime

from pydantic import BaseModel


class SummarySourceItem(BaseModel):
    title: str
    url: str
    published_at: datetime


class SummaryResponse(BaseModel):
    window_days: int
    start_date: date
    end_date: date
    article_count: int
    truncated: bool
    summary_text: str
    sources: list[SummarySourceItem]
    pipeline_running: bool
