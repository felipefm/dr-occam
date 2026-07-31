"""Sugestões de refinamento de busca via LLM.

Disparado quando a busca híbrida não encontra nenhum resultado acima do
piso de relevância (`search.config.MIN_RELEVANCE_PERCENTAGE`) — o caso
clássico de uma busca vaga demais (ex.: "atropelamento" sozinho, sem
contexto que ajude a discriminar qual atropelamento). As sugestões são
baseadas nos títulos que de fato apareceram no acervo (mesmo com baixa
relevância) — pistas reais do que existe, em vez de exemplos genéricos
inventados.

Reaproveita a mesma cascata de chat de intent_service.py — mesmo tipo de
tarefa curta, mesma cascata, mesmo timeout.
"""

import json
import logging
from datetime import date

import litellm

from llm_cascade import LLM_CASCADE
from search.config import INTENT_LLM_TIMEOUT_SECONDS
from search.llm_json_utils import extract_json_block

logger = logging.getLogger(__name__)

MAX_SUGGESTIONS = 4

_SYSTEM_PROMPT = (
    "Você ajuda o usuário de um agregador de notícias a refinar uma busca que "
    "não encontrou nenhum resultado relevante o suficiente. Você recebe a busca "
    "original e, se houver, títulos de artigos que até apareceram na busca mas "
    "com relevância baixa demais (pistas do que existe no acervo, não "
    "necessariamente o que o usuário quer). Sugira de 1 a "
    f"{MAX_SUGGESTIONS} buscas mais específicas.\n\n"
    "REGRA MAIS IMPORTANTE: cada sugestão deve ser, ela mesma, uma busca "
    "pronta para ser digitada e executada — NUNCA uma pergunta ao usuário, "
    "NUNCA uma instrução ou comentário sobre a busca. Errado: 'Você poderia "
    "especificar o local?' ou 'Tente buscar por X'. Certo: apenas 'X'. Cada "
    "sugestão é uma frase curta em linguagem natural (assunto + "
    "opcionalmente local/data/entidade), nada mais.\n\n"
    "Prefira acrescentar um local, período/data ou nome de pessoa/organização "
    "real que apareça nos títulos fornecidos. Se um filtro de data foi "
    "aplicado e pode estar sendo restritivo demais, uma das sugestões pode "
    "ser a própria busca original sem esse filtro. Se os títulos não derem "
    "nenhuma pista útil, sugira formas genéricas de especificar a busca "
    "original (local, período, entidade envolvida), sem inventar fatos que "
    "não estejam nos títulos. Responda SEMPRE em português do Brasil. "
    "Responda APENAS com um JSON válido no formato "
    '{"suggestions": ["...", "..."]}, sem markdown e sem texto adicional.'
)

_META_PREFIXES = (
    "tente", "você", "voce", "considere", "poderia", "seria", "que tal", "procure",
)


def _looks_like_query(text: str) -> bool:
    """Filtro defensivo: descarta sugestões que são perguntas ou comentários
    dirigidos ao usuário em vez de buscas prontas para executar.

    Necessário porque, na prática, o modelo local usado (poucos bilhões de
    parâmetros) às vezes ignora a instrução do prompt e gera meta-comentário
    mesmo assim (ex.: "Você poderia especificar...") — o front-end usa a
    sugestão como texto literal da próxima busca, então uma pergunta vazando
    aqui produz uma busca sem sentido (ver bug reportado com "telegram" e
    sugestões em formato de pergunta).
    """
    stripped = text.strip()
    if not stripped or stripped.endswith("?"):
        return False
    return not stripped.lower().startswith(_META_PREFIXES)


def _build_user_prompt(
    raw_query: str,
    weak_titles: list[str],
    start_date: date | None,
    end_date: date | None,
) -> str:
    lines = [f"Busca original: {raw_query}"]
    if start_date or end_date:
        lines.append(
            f"Filtro de data aplicado: {start_date or 'sem início'} até {end_date or 'hoje'}."
        )
    if weak_titles:
        lines.append("Títulos encontrados com baixa relevância:")
        lines.extend(f"- {title}" for title in weak_titles)
    else:
        lines.append("Nenhum artigo foi encontrado nem com baixa relevância.")
    return "\n".join(lines)


def _parse_suggestions(raw: str) -> list[str]:
    data = json.loads(extract_json_block(raw))
    suggestions = data.get("suggestions")
    if not isinstance(suggestions, list):
        return []
    cleaned = [s.strip() for s in suggestions if isinstance(s, str) and s.strip()]
    return [s for s in cleaned if _looks_like_query(s)][:MAX_SUGGESTIONS]


async def suggest_query_refinements(
    raw_query: str,
    weak_titles: list[str],
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[str]:
    """Pede à cascata de LLM sugestões de busca mais específicas.

    Em qualquer falha, retorna lista vazia — a ausência de sugestões não
    deve quebrar a resposta da busca, só deixa de mostrar essa ajuda extra
    (mesma filosofia de degradação graciosa de intent_service.py).
    """
    if not LLM_CASCADE:
        return []

    primary, *fallbacks = LLM_CASCADE
    primary_kwargs = {k: v for k, v in primary.items() if k != "model"}

    try:
        response = await litellm.acompletion(
            model=primary["model"],
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _build_user_prompt(raw_query, weak_titles, start_date, end_date),
                },
            ],
            temperature=0.4,
            timeout=INTENT_LLM_TIMEOUT_SECONDS,
            fallbacks=fallbacks or None,
            **primary_kwargs,
        )
        raw = response.choices[0].message.content or ""
        return _parse_suggestions(raw)
    except Exception:
        logger.exception("Sugestão de refinamento de busca falhou para %r", raw_query)
        return []
