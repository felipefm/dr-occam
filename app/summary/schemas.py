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


class PautaSummaryItem(BaseModel):
    id: int
    mode: str
    mode_label: str
    text: str
    article_ids: list[int]
    chosen: bool
    created_at: datetime


class PautaSummaryList(BaseModel):
    summaries: list[PautaSummaryItem]
    busy: bool


class PautaSummaryCreated(BaseModel):
    summary: PautaSummaryItem
    pipeline_running: bool
