"""Agendador interno do pipeline — roda dentro do próprio processo da
aplicação (nenhuma dependência do cron do SO). Usa APScheduler com um
`AsyncIOScheduler` acoplado ao mesmo event loop do FastAPI/uvicorn.

A configuração (ligado/desligado + lista de horários "HH:MM") é persistida
em `ScheduleConfig` (models.py); `start_scheduler()` a recarrega do banco
no startup da aplicação, então os agendamentos sobrevivem a um reinício do
container. `apply_schedule()` é chamada de novo sempre que o admin salva
uma nova configuração via `PUT /api/schedule`, reconfigurando os jobs em
tempo real sem precisar reiniciar o processo.

Pressupõe um único processo/worker uvicorn (ver Dockerfile) — com múltiplos
workers cada um teria seu próprio scheduler e o pipeline disparia em
duplicidade; se isso mudar no futuro, mova o agendamento para um processo
dedicado."""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session, select

from backup import config as backup_config
from backup.service import read_status, scheduled_backup
from database import engine
from models import ScheduleConfig
from pipeline import run_pipeline

logger = logging.getLogger(__name__)

# Mesmo fuso já usado para exibição no /admin e na home (ver main.py) —
# os horários configurados pelo usuário são sempre em horário de Brasília,
# independente do fuso do host/container.
SCHEDULER_TIMEZONE = ZoneInfo("America/Sao_Paulo")
JOB_ID_PREFIX = "pipeline-scan"
BACKUP_JOB_ID = "weekly-backup"
BACKUP_CATCHUP_JOB_ID = "backup-catchup"
# Atraso do backup de recuperação, para não disputar disco com o startup.
BACKUP_CATCHUP_DELAY = timedelta(minutes=10)

scheduler = AsyncIOScheduler(timezone=SCHEDULER_TIMEZONE)


def _parse_time(value: str) -> tuple[int, int]:
    hour_str, _, minute_str = value.partition(":")
    hour, minute = int(hour_str), int(minute_str)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Horário fora do intervalo válido: {value!r}")
    return hour, minute


async def _scheduled_run(scheduled_time: str) -> None:
    logger.info("Agendador: disparando varredura agendada (%s, America/Sao_Paulo)", scheduled_time)
    await run_pipeline()


def _clear_jobs() -> None:
    for job in scheduler.get_jobs():
        if job.id.startswith(JOB_ID_PREFIX):
            job.remove()


def apply_schedule(config: ScheduleConfig) -> None:
    """(Re)configura os jobs do agendador a partir de uma ScheduleConfig.

    Idempotente: sempre limpa os jobs anteriores antes de recriar, então
    pode ser chamada livremente após qualquer alteração salva no /admin.
    Não toca na lógica de ingestão/LLM em si — só decide *quando* chamar
    `run_pipeline()`.
    """
    _clear_jobs()

    if not config.enabled:
        logger.info("Agendador: modo manual ativo — nenhum job automático configurado")
        return

    for index, time_str in enumerate(config.times):
        hour, minute = _parse_time(time_str)
        job_id = f"{JOB_ID_PREFIX}-{index}"
        scheduler.add_job(
            _scheduled_run,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=SCHEDULER_TIMEZONE),
            args=[time_str],
            id=job_id,
            name=f"Varredura agendada {time_str}",
            replace_existing=True,
            coalesce=True,       # se o processo ficou fora do ar e perdeu disparos, roda só uma vez ao voltar
            max_instances=1,     # nunca duas execuções desse horário sobrepostas
            misfire_grace_time=3600,
        )
        logger.info("Agendador: job '%s' configurado para %s (America/Sao_Paulo)", job_id, time_str)


def load_schedule_from_db() -> ScheduleConfig:
    """Lê a configuração persistida, criando a linha padrão (modo manual)
    na primeira execução da aplicação."""
    with Session(engine) as session:
        config = session.exec(select(ScheduleConfig)).first()
        if config is None:
            config = ScheduleConfig(enabled=False, times=[])
            session.add(config)
            session.commit()
            session.refresh(config)
        return config


def _schedule_backup() -> None:
    """Backup semanal (backup/config.py). Independe do modo manual das
    varreduras e não é afetado por `apply_schedule` (prefixo de id diferente)."""
    if not backup_config.BACKUP_ENABLED:
        logger.info("Agendador: backup semanal desligado (BACKUP_ENABLED=false)")
        return
    hour, minute = _parse_time(backup_config.BACKUP_TIME)
    scheduler.add_job(
        scheduled_backup,
        trigger=CronTrigger(
            day_of_week=backup_config.BACKUP_WEEKDAY, hour=hour, minute=minute,
            timezone=SCHEDULER_TIMEZONE,
        ),
        id=BACKUP_JOB_ID,
        name="Backup semanal do banco",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    logger.info(
        "Agendador: backup semanal às %s de %s (America/Sao_Paulo)",
        backup_config.BACKUP_TIME, backup_config.BACKUP_WEEKDAY,
    )
    _schedule_backup_catchup()


def _schedule_backup_catchup() -> None:
    """Os jobs ficam só em memória: se o container estava fora do ar no
    horário do backup, o disparo se perde. Ao subir, se o último backup
    bem-sucedido tem mais de 7 dias (ou nunca houve), agenda um avulso."""
    last = (read_status().get("last_success") or {}).get("at")
    try:
        last_at = datetime.fromisoformat(last) if last else None
    except ValueError:
        last_at = None
    now = datetime.now(timezone.utc)
    if last_at and now - last_at < timedelta(days=7, hours=1):
        return
    run_at = now + BACKUP_CATCHUP_DELAY
    scheduler.add_job(
        scheduled_backup,
        trigger="date",
        run_date=run_at,
        id=BACKUP_CATCHUP_JOB_ID,
        name="Backup de recuperação (último com mais de 7 dias)",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info(
        "Agendador: último backup %s; backup de recuperação em %d min",
        f"em {last_at:%d/%m/%Y}" if last_at else "nunca feito",
        BACKUP_CATCHUP_DELAY.seconds // 60,
    )


def next_backup_run():
    """Próximo disparo de backup, semanal ou de recuperação (datetime com
    fuso), ou None."""
    runs = [
        job.next_run_time
        for job in (scheduler.get_job(BACKUP_JOB_ID), scheduler.get_job(BACKUP_CATCHUP_JOB_ID))
        if job and job.next_run_time
    ]
    return min(runs) if runs else None


def start_scheduler() -> None:
    config = load_schedule_from_db()
    apply_schedule(config)
    _schedule_backup()
    if not scheduler.running:
        scheduler.start()
    logger.info(
        "Agendador iniciado (enabled=%s, times=%s)", config.enabled, config.times
    )


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
