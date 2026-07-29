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
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session, select

from database import engine
from models import ScheduleConfig
from pipeline import run_pipeline

logger = logging.getLogger(__name__)

# Mesmo fuso já usado para exibição no /admin e na home (ver main.py) —
# os horários configurados pelo usuário são sempre em horário de Brasília,
# independente do fuso do host/container.
SCHEDULER_TIMEZONE = ZoneInfo("America/Sao_Paulo")
JOB_ID_PREFIX = "pipeline-scan"

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


def start_scheduler() -> None:
    config = load_schedule_from_db()
    apply_schedule(config)
    if not scheduler.running:
        scheduler.start()
    logger.info(
        "Agendador iniciado (enabled=%s, times=%s)", config.enabled, config.times
    )


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
