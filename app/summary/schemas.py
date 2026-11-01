"""Schemas Pydantic do resumo por IA da pauta."""

from datetime import datetime

from pydantic import BaseModel


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
