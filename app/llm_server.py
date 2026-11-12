"""Servidor de IA local (LM Studio): endereço e modelo de chat editáveis no /admin.

O compose define o endereço em LLM_API_BASE_1 (normalmente repetido em
EMBEDDING_API_BASE) e o modelo em LLM_MODEL_1. Quando o IP da máquina muda
ou se quer experimentar outro modelo, em vez de editar o compose e recriar o
container, salva-se no /admin: fica na tabela `llm_server_config` e passa a
valer na próxima chamada, sem reiniciar nada.

Regras:
- Endereço: o "servidor local" é o valor de LLM_API_BASE_1 no ambiente.
  Todo lugar configurado com esse mesmo endereço (qualquer LLM_API_BASE_N e
  o EMBEDDING_API_BASE) passa a usar o endereço salvo. Endereços diferentes
  (ex.: um provedor na nuvem) não são tocados.
- Modelo: troca o modelo da primeira entrada da cascata que aponta para o
  servidor local (hoje, LLM_MODEL_1). Fallbacks seguem os do compose.
- Endereço e modelo são independentes; o que não estiver salvo vale o do
  ambiente, como antes.

O modelo de embedding NÃO é editável: os vetores gravados dependem dele
(trocar exigiria recalcular todos). Chaves também ficam no compose/.env —
no banco iriam parar no backup e na página.
"""

import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ENV_LOCAL_API_BASE: str | None = (os.getenv("LLM_API_BASE_1") or "").rstrip("/") or None

_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+(:\d{1,5})?(/[^\s]*)?$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:@/\-]{1,200}$")


@dataclass(frozen=True)
class Overrides:
    api_base: str | None = None
    model: str | None = None  # id como o servidor lista (ex.: "google/gemma-3-12b")
    updated_at: datetime | None = None


_lock = threading.Lock()
_cache: Overrides | None = None


def normalize_api_base(value: str) -> str:
    """Valida e normaliza (sem barra no fim). Levanta ValueError se inválido."""
    value = value.strip().rstrip("/")
    if not _URL_RE.match(value):
        raise ValueError(
            "Endereço inválido — use o formato http://IP:PORTA/v1 (ex.: http://192.168.0.15:1234/v1)"
        )
    return value


def normalize_model(value: str) -> str:
    value = value.strip()
    if not _MODEL_RE.match(value):
        raise ValueError("Nome de modelo inválido")
    return value


def server_model_id(litellm_model: str) -> str:
    """"openai/google/gemma-4-e4b" → "google/gemma-4-e4b": o prefixo só diz
    ao LiteLLM que a API é compatível com a da OpenAI; o servidor não o usa."""
    return litellm_model.split("/", 1)[1] if litellm_model.startswith("openai/") else litellm_model


def to_litellm_model(model_id: str, env_model: str) -> str:
    """Põe de volta o prefixo de provedor que o modelo do compose usa."""
    return f"openai/{model_id}" if env_model.startswith("openai/") else model_id


def _load_from_db() -> Overrides:
    # Import tardio: database importa embeddings, que importa este módulo.
    from sqlmodel import Session, select

    from database import engine
    from models import LLMServerConfig

    try:
        with Session(engine) as session:
            row = session.exec(select(LLMServerConfig)).first()
            if row is None:
                return Overrides()
            return Overrides(api_base=row.api_base, model=row.model, updated_at=row.updated_at)
    except Exception:  # tabela ainda não criada, banco travado etc.
        logger.exception("Não foi possível ler o servidor de IA do banco; usando o do ambiente")
        return Overrides()


def get_overrides() -> Overrides:
    """O que foi salvo no /admin. Lido do banco uma vez e mantido em memória;
    `save_overrides` atualiza o cache."""
    global _cache
    if _cache is None:
        with _lock:
            if _cache is None:
                _cache = _load_from_db()
    return _cache


def effective_local_api_base() -> str | None:
    return get_overrides().api_base or ENV_LOCAL_API_BASE


def is_local(env_api_base: str | None) -> bool:
    return bool(env_api_base and ENV_LOCAL_API_BASE) and env_api_base.rstrip("/") == ENV_LOCAL_API_BASE


def resolve_api_base(env_value: str | None) -> str | None:
    """Troca um endereço vindo do ambiente pelo salvo no /admin quando ele
    aponta para o servidor local (mesmo valor de LLM_API_BASE_1)."""
    if not is_local(env_value):
        return env_value
    return get_overrides().api_base or env_value


def save_overrides(api_base: str | None, model: str | None) -> Overrides:
    """Grava os dois campos (None = vale o do ambiente). Sem nenhum dos dois,
    apaga a linha. Atualiza o cache e retorna o que ficou."""
    global _cache
    from sqlmodel import Session, select

    from database import engine
    from models import LLMServerConfig

    now = datetime.now(timezone.utc)
    with _lock:
        with Session(engine) as session:
            row = session.exec(select(LLMServerConfig)).first()
            if api_base is None and model is None:
                if row is not None:
                    session.delete(row)
                result = Overrides()
            else:
                row = row or LLMServerConfig()
                row.api_base = api_base
                row.model = model
                row.updated_at = now
                session.add(row)
                result = Overrides(api_base=api_base, model=model, updated_at=now)
            session.commit()
        _cache = result
    return result
