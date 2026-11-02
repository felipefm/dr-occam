"""Cópia do banco, retenção e status do backup.

A cópia usa a API de backup do SQLite (`sqlite3.Connection.backup`), que
lê um retrato consistente do banco mesmo com o sistema no ar: o banco está
em modo WAL, então a cópia não trava a varredura nem é corrompida por ela.
É a mesma cópia usada antes de cada deploy.

Passos:
1. confere se há espaço em disco (o tamanho do banco + 20%);
2. copia para `occam-AAAA-MM-DD-HHMMSS.db.partial`;
3. deixa a cópia em modo de journal normal (um arquivo só, sem -wal/-shm,
   fácil de mover) e roda `PRAGMA quick_check`;
4. só então renomeia para `.db`; um arquivo `.db` na pasta é sempre uma
   cópia completa e conferida;
5. apaga as cópias mais antigas, mantendo as `BACKUP_KEEP` mais novas.

O status (último sucesso e último erro) fica em `backup-status.json`, ao
lado do banco e fora da pasta de backups, para continuar valendo depois
que o script do usuário mover as cópias para outro lugar.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pipeline_state
from backup import config
from database import DATABASE_URL
from timezone_utils import DISPLAY_TIMEZONE

logger = logging.getLogger(__name__)

# Só arquivos com este nome exato são listados e apagados pela retenção;
# qualquer outro arquivo na pasta é ignorado.
BACKUP_FILE_RE = re.compile(r"^occam-(\d{4}-\d{2}-\d{2})-(\d{6})\.db$")
PARTIAL_SUFFIX = ".partial"
WAIT_POLL_SECONDS = 60

_lock = asyncio.Lock()
_waiting_for_pipeline = False
# Referência à tarefa do backup manual, para o event loop não descartá-la.
_manual_task: asyncio.Task | None = None


class BackupBusyError(Exception):
    """Já há um backup em andamento (ou esperando a varredura terminar)."""


class BackupError(Exception):
    """Falha na cópia: sem espaço, erro de E/S ou cópia que não passou na conferência."""


@dataclass
class BackupFile:
    name: str
    size: int
    created_at: datetime


def _match_owner(path: Path) -> None:
    """Dá a `path` o mesmo dono da pasta do banco. O container roda como
    root, mas a pasta `app/data` no host é do usuário: sem isso, o script
    dele não conseguiria mover nem apagar as cópias."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    ref = database_path().parent.stat()
    try:
        os.chown(path, ref.st_uid, ref.st_gid)
    except OSError:
        logger.warning("Backup: não foi possível ajustar o dono de %s", path, exc_info=True)


def database_path() -> Path:
    prefix = "sqlite:///"
    if not DATABASE_URL.startswith(prefix):
        raise BackupError("O backup só funciona com banco SQLite")
    return Path(DATABASE_URL[len(prefix):]).resolve()


def backup_dir() -> Path:
    if config.BACKUP_DIR:
        return Path(config.BACKUP_DIR).resolve()
    return database_path().parent / "backups"


def _status_path() -> Path:
    return database_path().parent / "backup-status.json"


def read_status() -> dict:
    try:
        return json.loads(_status_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("Backup: não foi possível ler %s", _status_path(), exc_info=True)
        return {}


def _write_status(**changes: dict | None) -> None:
    status = read_status()
    status.update(changes)
    path = _status_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    _match_owner(tmp)
    os.replace(tmp, path)


def list_backups() -> list[BackupFile]:
    """Cópias completas que estão na pasta, da mais nova para a mais antiga."""
    folder = backup_dir()
    if not folder.is_dir():
        return []
    files = []
    for entry in folder.iterdir():
        match = BACKUP_FILE_RE.match(entry.name)
        if not match or not entry.is_file():
            continue
        stamp = datetime.strptime(f"{match[1]} {match[2]}", "%Y-%m-%d %H%M%S")
        files.append(
            BackupFile(
                name=entry.name,
                size=entry.stat().st_size,
                created_at=stamp.replace(tzinfo=DISPLAY_TIMEZONE),
            )
        )
    return sorted(files, key=lambda f: f.name, reverse=True)


def is_busy() -> bool:
    return _lock.locked()


def is_waiting_for_pipeline() -> bool:
    return _waiting_for_pipeline


def _copy_database(dest: Path) -> int:
    """Copia o banco para `dest` (síncrono; roda numa thread). Devolve o tamanho."""
    source_path = database_path()
    folder = dest.parent
    folder.mkdir(parents=True, exist_ok=True)
    _match_owner(folder)

    needed = source_path.stat().st_size
    wal = source_path.with_name(source_path.name + "-wal")
    if wal.exists():
        needed += wal.stat().st_size
    free = shutil.disk_usage(folder).free
    if free < needed * 1.2:
        raise BackupError(
            f"Pouco espaço em disco: {free // 2**20} MB livres, a cópia precisa de ~{needed // 2**20} MB"
        )

    partial = dest.with_name(dest.name + PARTIAL_SUFFIX)
    partial.unlink(missing_ok=True)
    try:
        source = sqlite3.connect(str(source_path), timeout=30)
        try:
            target = sqlite3.connect(str(partial))
            try:
                # pages=-1 (padrão): copia tudo num passo só. Em passos, uma
                # escrita da varredura no meio faria a cópia recomeçar.
                source.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")
                result = target.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                target.close()
        finally:
            source.close()
        if result != "ok":
            raise BackupError(f"A cópia não passou na conferência (quick_check: {result})")
        _match_owner(partial)
        os.replace(partial, dest)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return dest.stat().st_size


def _apply_retention() -> list[str]:
    removed = []
    for old in list_backups()[config.BACKUP_KEEP:]:
        try:
            (backup_dir() / old.name).unlink()
            removed.append(old.name)
        except OSError:
            logger.warning("Backup: não foi possível apagar a cópia antiga %s", old.name, exc_info=True)
    # Sobras de uma cópia interrompida (ex.: container reiniciado no meio).
    folder = backup_dir()
    for leftover in folder.glob("occam-*.db" + PARTIAL_SUFFIX):
        leftover.unlink(missing_ok=True)
    return removed


def _backup_now(trigger: str) -> dict:
    started = time.monotonic()
    now_local = datetime.now(DISPLAY_TIMEZONE)
    dest = backup_dir() / f"occam-{now_local:%Y-%m-%d-%H%M%S}.db"
    if dest.exists():
        raise BackupError(f"Já existe uma cópia com este nome: {dest.name} (tente de novo em instantes)")
    size = _copy_database(dest)
    removed = _apply_retention()
    seconds = round(time.monotonic() - started, 1)
    logger.info(
        "Backup (%s): %s criado (%.1f MB, %.1fs); cópias antigas removidas: %s",
        trigger, dest.name, size / 2**20, seconds, ", ".join(removed) or "nenhuma",
    )
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "file": dest.name,
        "size": size,
        "seconds": seconds,
        "trigger": trigger,
        "removed": removed,
    }


async def _wait_for_pipeline() -> None:
    global _waiting_for_pipeline
    deadline = time.monotonic() + config.BACKUP_MAX_WAIT_MINUTES * 60
    if pipeline_state.is_running():
        logger.info("Backup: varredura em andamento, aguardando terminar")
    while pipeline_state.is_running() and time.monotonic() < deadline:
        _waiting_for_pipeline = True
        await asyncio.sleep(WAIT_POLL_SECONDS)
    if pipeline_state.is_running():
        logger.warning("Backup: varredura ainda rodando após %d min, copiando mesmo assim", config.BACKUP_MAX_WAIT_MINUTES)
    _waiting_for_pipeline = False


async def run_backup(trigger: str) -> dict:
    """Faz um backup. `trigger`: "agendado" (espera a varredura terminar)
    ou "manual" (copia na hora). Levanta BackupBusyError se já houver um
    em andamento, e BackupError (ou OSError/sqlite3.Error) se falhar; a
    falha também fica registrada no status."""
    global _waiting_for_pipeline
    if _lock.locked():
        raise BackupBusyError("Já há um backup em andamento")
    async with _lock:
        try:
            if trigger == "agendado":
                await _wait_for_pipeline()
            result = await asyncio.to_thread(_backup_now, trigger)
        except Exception as exc:
            logger.exception("Backup (%s) falhou", trigger)
            _write_status(
                last_error={
                    "at": datetime.now(timezone.utc).isoformat(),
                    "message": str(exc) or exc.__class__.__name__,
                    "trigger": trigger,
                }
            )
            raise
        finally:
            _waiting_for_pipeline = False
        _write_status(last_success=result)
        return result


async def scheduled_backup() -> None:
    """Ponto de entrada do agendador: erros já ficam no log e no status."""
    try:
        await run_backup("agendado")
    except BackupBusyError:
        logger.info("Backup agendado: já havia um backup em andamento, nada a fazer")
    except Exception:
        pass


def start_manual_backup() -> None:
    """Dispara um backup manual em segundo plano (o /admin acompanha pelo
    GET /api/backup). Levanta BackupBusyError se já houver um rodando."""
    global _manual_task
    if _lock.locked():
        raise BackupBusyError("Já há um backup em andamento")

    async def _run() -> None:
        try:
            await run_backup("manual")
        except Exception:
            pass

    _manual_task = asyncio.create_task(_run())
