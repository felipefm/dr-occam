"""Schemas Pydantic do resumo por IA da pauta."""

from datetime import datetime

from pydantic import BaseModel


class PautaSummaryItem(BaseModel):
    id: int
    mode: str
    mode_label: str
    prompt: str | None  # prompt editável usado (NULL em versões antigas)
    prompt_outdated: bool  # o prompt do formato foi editado depois desta versão
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


class SummaryFormatItem(BaseModel):
    id: int
    key: str
    label: str
    prompt: str
    position: int
    active: bool
    builtin: bool  # formato de fábrica: tem "Restaurar padrão" e não pode ser apagado
    is_default: bool  # nome e prompt iguais aos de fábrica
    updated_at: datetime


class SummaryFormatList(BaseModel):
    formats: list[SummaryFormatItem]
    fixed_rules: str
