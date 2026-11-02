"""Backup semanal do banco SQLite (ver docs/backup.md).

    config: dia, horário, quantas cópias guardar e pasta (variáveis de ambiente).
    service: a cópia em si (API de backup do SQLite), retenção e status.
    router: GET/POST /api/backup, usados pela seção "Backup" do /admin.

O agendamento mora em scheduler.py, junto com o das varreduras.
"""
