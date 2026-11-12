"""Rotas da seção "Servidor de IA" do /admin (ver llm_server.py)."""

import logging
import time
from datetime import datetime

import httpx
import litellm
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import llm_server
from embeddings.config import EMBEDDING_API_BASE, EMBEDDING_MODEL
from llm_cascade import env_local_entry, get_llm_cascade, load_llm_cascade
from llm_processor import LLM_TIMEOUT_SECONDS, SYSTEM_PROMPT, _parse_completion

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Servidor de IA"])

LIST_TIMEOUT_SECONDS = 5.0

# Notícia curta de exemplo para o teste: mesmo prompt e mesma leitura da
# resposta que o processamento real dos artigos (llm_processor.py).
_SAMPLE_ARTICLE = (
    "A prefeitura de Curitiba anunciou nesta terça-feira a ampliação do horário de "
    "funcionamento de 12 unidades de saúde, que passarão a atender até as 22h a partir "
    "do próximo mês. Segundo a secretaria municipal, a medida deve beneficiar cerca de "
    "300 mil moradores e custará R$ 18 milhões por ano. O sindicato dos servidores "
    "afirmou que vai acompanhar a contratação dos 140 profissionais previstos."
)


class LLMServerStatus(BaseModel):
    api_base: str | None
    env_api_base: str | None
    api_base_overridden: bool
    model: str | None
    env_model: str | None
    model_overridden: bool
    embedding_model: str | None
    updated_at: datetime | None
    uses: list[str]


class LLMServerPayload(BaseModel):
    api_base: str | None = None
    model: str | None = None


class LLMServerModelsPayload(BaseModel):
    api_base: str


class LLMServerModels(BaseModel):
    ok: bool
    message: str
    chat_models: list[str]
    loaded: list[str] = []
    embedding_found: bool | None


class LLMServerTestPayload(BaseModel):
    api_base: str
    model: str


class LLMServerTestResult(BaseModel):
    ok: bool
    message: str
    seconds: float | None = None
    title: str | None = None
    summary: str | None = None
    warnings: list[str] = []


def _env_model_id() -> str | None:
    entry = env_local_entry()
    return llm_server.server_model_id(entry["model"]) if entry else None


def _embedding_model_id() -> str | None:
    return llm_server.server_model_id(EMBEDDING_MODEL) if llm_server.is_local(EMBEDDING_API_BASE) else None


def _status() -> LLMServerStatus:
    saved = llm_server.get_overrides()
    env_model = _env_model_id()
    cascade = get_llm_cascade()
    uses = [
        f"Modelo {n} da cascata{' (resumos, busca)' if n == 1 else ' (reserva)'}: {entry['model']}"
        for n, (entry, env_entry) in enumerate(zip(cascade, load_llm_cascade()), start=1)
        if llm_server.is_local(env_entry.get("api_base"))
    ]
    if _embedding_model_id():
        uses.append(f"Embeddings (busca): {EMBEDDING_MODEL}")
    return LLMServerStatus(
        api_base=llm_server.effective_local_api_base(),
        env_api_base=llm_server.ENV_LOCAL_API_BASE,
        api_base_overridden=saved.api_base is not None,
        model=saved.model or env_model,
        env_model=env_model,
        model_overridden=saved.model is not None,
        embedding_model=_embedding_model_id(),
        updated_at=saved.updated_at,
        uses=uses,
    )


def _api_base_or_422(value: str) -> str:
    try:
        return llm_server.normalize_api_base(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _list_server_models(api_base: str) -> list[str]:
    """Ids que o servidor lista em /models. Levanta ValueError com mensagem
    pronta para a tela."""
    try:
        async with httpx.AsyncClient(timeout=LIST_TIMEOUT_SECONDS) as client:
            res = await client.get(f"{api_base}/models")
        res.raise_for_status()
        return [m["id"] for m in res.json().get("data", []) if isinstance(m, dict) and m.get("id")]
    except httpx.TimeoutException as exc:
        raise ValueError(
            f"Sem resposta em {LIST_TIMEOUT_SECONDS:.0f}s — a máquina está ligada e o IP está certo?"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"O servidor respondeu com erro {exc.response.status_code}") from exc
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise ValueError(f"Não foi possível conectar: {exc.__class__.__name__}") from exc


async def _loaded_models(api_base: str) -> list[str]:
    """Modelos carregados agora, pela API própria do LM Studio
    (/api/v0/models). Outro servidor ou versão sem essa API → lista vazia."""
    if not api_base.endswith("/v1"):
        return []
    try:
        async with httpx.AsyncClient(timeout=LIST_TIMEOUT_SECONDS) as client:
            res = await client.get(api_base[: -len("/v1")] + "/api/v0/models")
        res.raise_for_status()
        return [
            m["id"] for m in res.json().get("data", [])
            if isinstance(m, dict) and m.get("state") == "loaded" and m.get("id")
        ]
    except (httpx.HTTPError, ValueError, KeyError):
        return []


@router.get(
    "/api/llm-server",
    summary="Servidor de IA local: endereço e modelo em uso",
    description=(
        "Endereço e modelo de chat em uso (os salvos no /admin ou, sem eles, LLM_API_BASE_1 "
        "e LLM_MODEL_1 do ambiente), os valores do ambiente, o modelo de embedding (só "
        "leitura) e o que usa esse servidor."
    ),
)
def get_llm_server() -> LLMServerStatus:
    return _status()


@router.put(
    "/api/llm-server",
    summary="Trocar endereço e/ou modelo do servidor de IA local",
    description=(
        "Grava endereço e modelo de chat; valem a partir da próxima chamada, sem reiniciar. "
        "Campo vazio, nulo ou igual ao do ambiente = volta ao do ambiente. O endereço vale "
        "também para os embeddings; o modelo de embedding não muda."
    ),
)
def update_llm_server(payload: LLMServerPayload) -> LLMServerStatus:
    if not llm_server.ENV_LOCAL_API_BASE:
        raise HTTPException(
            status_code=409,
            detail="LLM_API_BASE_1 não está definido no ambiente — não há servidor local para trocar",
        )
    api_base = _api_base_or_422(payload.api_base) if (payload.api_base or "").strip() else None
    model = None
    if (payload.model or "").strip():
        try:
            model = llm_server.normalize_model(payload.model)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Igual ao ambiente = não precisa guardar.
    if api_base == llm_server.ENV_LOCAL_API_BASE:
        api_base = None
    if model == _env_model_id():
        model = None
    llm_server.save_overrides(api_base, model)
    logger.info(
        "Servidor de IA alterado via /admin: endereço=%s, modelo=%s",
        api_base or f"do ambiente ({llm_server.ENV_LOCAL_API_BASE})",
        model or f"do ambiente ({_env_model_id()})",
    )
    return _status()


@router.post(
    "/api/llm-server/models",
    summary="Listar os modelos de um servidor de IA",
    description=(
        "Consulta `GET <api_base>/models` e devolve os modelos de chat (os de embedding "
        "ficam de fora) e se o modelo de embedding configurado está lá. Não grava nada."
    ),
)
async def list_llm_server_models(payload: LLMServerModelsPayload) -> LLMServerModels:
    api_base = _api_base_or_422(payload.api_base)
    try:
        models = await _list_server_models(api_base)
    except ValueError as exc:
        return LLMServerModels(ok=False, message=str(exc), chat_models=[], embedding_found=None)

    embedding = _embedding_model_id()
    chat_models = [m for m in models if m != embedding and "embed" not in m.lower()]
    found = (embedding in models) if embedding else None
    message = f"Servidor respondeu com {len(chat_models)} modelo(s) de chat"
    if found is False:
        message += f"; o modelo de embedding {embedding} não está na lista (a busca vai falhar)"
    return LLMServerModels(
        ok=found is not False, message=message, chat_models=chat_models,
        loaded=await _loaded_models(api_base), embedding_found=found,
    )


@router.post(
    "/api/llm-server/test",
    summary="Testar um modelo com uma notícia de exemplo",
    description=(
        "Manda uma notícia curta de exemplo ao modelo, com o mesmo prompt do processamento "
        "real dos artigos, e confere se a resposta vem no formato esperado (JSON com título "
        "e resumo). Sem fallback: testa só o modelo pedido. Não grava nada. Se o modelo não "
        "estiver carregado, o LM Studio o carrega agora, o que pode levar um minuto."
    ),
)
async def test_llm_server(payload: LLMServerTestPayload) -> LLMServerTestResult:
    api_base = _api_base_or_422(payload.api_base)
    try:
        model_id = llm_server.normalize_model(payload.model)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    entry = env_local_entry()
    if entry is None:
        raise HTTPException(status_code=409, detail="Nenhum modelo da cascata aponta para o servidor local")

    started = time.monotonic()
    try:
        response = await litellm.acompletion(
            model=llm_server.to_litellm_model(model_id, entry["model"]),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _SAMPLE_ARTICLE},
            ],
            temperature=0.2,
            timeout=LLM_TIMEOUT_SECONDS,
            api_base=api_base,
            api_key=entry.get("api_key"),
        )
        raw = response.choices[0].message.content or ""
        answered_by = llm_server.server_model_id(str(getattr(response, "model", "") or ""))
    except litellm.Timeout:
        return LLMServerTestResult(ok=False, message=f"O modelo não respondeu em {LLM_TIMEOUT_SECONDS}s")
    except Exception as exc:
        logger.warning("Teste do servidor de IA falhou (%s em %s): %s", model_id, api_base, exc)
        return LLMServerTestResult(ok=False, message=f"Falhou: {str(exc)[:300]}")
    seconds = round(time.monotonic() - started, 1)

    # O LM Studio, ao receber um nome que não conhece (ou que não consegue
    # carregar), responde em silêncio com o modelo que estiver carregado.
    if answered_by and answered_by != model_id:
        return LLMServerTestResult(
            ok=False, seconds=seconds,
            message=(
                f"Quem respondeu foi {answered_by}, não {model_id}: o servidor não tem esse modelo "
                "ou não o carregou sozinho. No LM Studio, carregue o modelo (ou ative o "
                "carregamento sob demanda) e teste de novo"
            ),
        )

    warnings: list[str] = []
    if "<think>" in raw or "</think>" in raw:
        warnings.append(
            "O modelo escreve o raciocínio antes da resposta: é mais lento e gasta mais tokens por notícia."
        )
    try:
        title, summary = _parse_completion(raw)
    except ValueError as exc:
        return LLMServerTestResult(
            ok=False, seconds=seconds, warnings=warnings,
            message=f"Respondeu em {seconds}s, mas fora do formato esperado ({exc}). As notícias falhariam.",
        )
    return LLMServerTestResult(
        ok=True, seconds=seconds, title=title, summary=summary, warnings=warnings,
        message=f"Respondeu em {seconds}s no formato certo",
    )
