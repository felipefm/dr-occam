"""Configuração do backup semanal, por variáveis de ambiente.

    BACKUP_ENABLED: "false" desliga o agendamento (o botão do /admin
        continua funcionando).
    BACKUP_WEEKDAY: dia da semana, no formato do APScheduler
        (mon, tue, wed, thu, fri, sat, sun). Padrão: domingo.
    BACKUP_TIME: horário "HH:MM", em horário de Brasília. Padrão: 03:30.
    BACKUP_KEEP: quantas cópias manter na pasta (as mais novas). Padrão: 4.
        Só conta o que ainda está na pasta: o que for movido para fora não
        é tocado.
    BACKUP_DIR: pasta das cópias. Padrão: "backups" ao lado do banco
        (app/data/backups no host).
    BACKUP_MAX_WAIT_MINUTES: quanto o backup agendado espera uma varredura
        em andamento terminar antes de copiar mesmo assim (a cópia é segura
        com o sistema no ar; esperar só evita disputar disco). Padrão: 180.
"""

import os

WEEKDAY_LABELS = {
    "mon": "segunda-feira",
    "tue": "terça-feira",
    "wed": "quarta-feira",
    "thu": "quinta-feira",
    "fri": "sexta-feira",
    "sat": "sábado",
    "sun": "domingo",
}

BACKUP_ENABLED: bool = os.getenv("BACKUP_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
BACKUP_WEEKDAY: str = os.getenv("BACKUP_WEEKDAY", "sun").strip().lower()
BACKUP_TIME: str = os.getenv("BACKUP_TIME", "03:30").strip()
BACKUP_KEEP: int = max(1, int(os.getenv("BACKUP_KEEP", "4")))
BACKUP_DIR: str | None = os.getenv("BACKUP_DIR") or None
BACKUP_MAX_WAIT_MINUTES: int = max(0, int(os.getenv("BACKUP_MAX_WAIT_MINUTES", "180")))

if BACKUP_WEEKDAY not in WEEKDAY_LABELS:
    raise ValueError(f"BACKUP_WEEKDAY inválido: {BACKUP_WEEKDAY!r} (use {', '.join(WEEKDAY_LABELS)})")
