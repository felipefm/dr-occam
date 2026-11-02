"""Rotas do backup semanal, usadas pela seção "Backup" do /admin."""

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import scheduler as scheduler_service
from backup import config, service

router = APIRouter(tags=["Backup"])


class BackupFileItem(BaseModel):
    name: str
    size: int
    created_at: datetime


class BackupStatus(BaseModel):
    enabled: bool
    schedule_label: str
    keep: int
    directory: str
    next_run: datetime | None
    running: bool
    waiting_for_pipeline: bool
    last_success: dict | None
    last_error: dict | None
    files: list[BackupFileItem]


def _schedule_label() -> str:
    day = config.WEEKDAY_LABELS[config.BACKUP_WEEKDAY]
    every = "Toda" if day.endswith("feira") else "Todo"
    return f"{every} {day} às {config.BACKUP_TIME}"


def _status() -> BackupStatus:
    saved = service.read_status()
    last_success = saved.get("last_success")
    last_error = saved.get("last_error")
    # Um erro antigo, já seguido de um backup bem-sucedido, não interessa mais.
    if last_error and last_success and last_success.get("at", "") > last_error.get("at", ""):
        last_error = None
    return BackupStatus(
        enabled=config.BACKUP_ENABLED,
        schedule_label=_schedule_label(),
        keep=config.BACKUP_KEEP,
        directory=str(service.backup_dir()),
        next_run=scheduler_service.next_backup_run(),
        running=service.is_busy(),
        waiting_for_pipeline=service.is_waiting_for_pipeline(),
        last_success=last_success,
        last_error=last_error,
        files=[BackupFileItem(**vars(f)) for f in service.list_backups()],
    )


@router.get(
    "/api/backup",
    summary="Situação do backup semanal",
    description=(
        "Configuração (dia, horário, quantas cópias guardar, pasta), próximo disparo, "
        "último backup bem-sucedido, último erro (se for mais recente que o último "
        "sucesso) e as cópias que estão na pasta."
    ),
)
def get_backup_status() -> BackupStatus:
    return _status()


@router.post(
    "/api/backup",
    status_code=202,
    summary="Fazer um backup agora",
    description=(
        "Dispara uma cópia do banco em segundo plano, sem esperar a varredura. "
        "Acompanhe por `GET /api/backup` (`running`). 409 se já houver um backup em andamento."
    ),
)
async def start_backup() -> BackupStatus:
    try:
        service.start_manual_backup()
    except service.BackupBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # A tarefa acabou de ser criada e ainda não pegou a trava.
    return _status().model_copy(update={"running": True})
