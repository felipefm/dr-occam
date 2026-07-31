"""Extração de intenção de filtro de data a partir de uma busca em
linguagem natural, via LLM.

Reaproveita a mesma cascata de modelos de chat já configurada para o
resumo dos artigos (`llm_cascade.py`) — é o mesmo tipo de tarefa
(chat/instruct), não precisa de uma cascata própria nem de outra chave de
API. Falhas aqui (timeout, JSON malformado, cascata inteira fora do ar)
degradam graciosamente para "sem filtro de data": a busca semântica pura
ainda deve funcionar mesmo se esta etapa quebrar.
"""

import json
import logging
from datetime import date, datetime

import litellm

from llm_cascade import LLM_CASCADE
from search.config import INTENT_LLM_TIMEOUT_SECONDS
from search.llm_json_utils import extract_json_block
from search.schemas import DateIntent
from timezone_utils import DISPLAY_TIMEZONE

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_TEMPLATE = (
    "Você extrai metadados estruturados de uma busca em linguagem natural feita "
    "num agregador de notícias. A data de hoje é {today} (formato AAAA-MM-DD). "
    "Se a busca mencionar um período de tempo (ex.: 'últimos 30 dias', 'essa "
    "semana', 'em janeiro', 'ontem', 'este ano'), calcule start_date e end_date "
    "no formato AAAA-MM-DD, como datas absolutas relativas à data de hoje "
    "informada, seguindo estas regras EXATAS (não arredonde nem simplifique "
    "para um único dia):\n"
    "- 'últimos N dias': start_date = hoje menos N dias, end_date = hoje.\n"
    "- 'essa semana'/'esta semana': start_date = segunda-feira desta semana, "
    "end_date = hoje.\n"
    "- 'semana passada': start_date = segunda-feira da semana anterior, "
    "end_date = domingo da semana anterior (intervalo de 7 dias).\n"
    "- 'mês passado': start_date = primeiro dia do mês calendário anterior, "
    "end_date = último dia desse MESMO mês anterior — é o mês inteiro "
    "(28-31 dias), NUNCA um único dia.\n"
    "- 'este mês': start_date = primeiro dia do mês atual, end_date = hoje.\n"
    "- 'ano passado': start_date = 1º de janeiro do ano anterior, end_date = "
    "31 de dezembro desse mesmo ano anterior — o ano inteiro.\n"
    "- 'ontem': start_date = end_date = hoje menos 1 dia (esse caso, e só "
    "esse, é um único dia).\n"
    "- Um mês específico nomeado (ex.: 'em janeiro'): primeiro e último dia "
    "desse mês, no ano mais recente em que esse mês já terminou.\n"
    "Se a busca não mencionar nenhum período de tempo, retorne start_date e "
    "end_date como null. Em cleaned_query, repita a busca original SEM a "
    "parte temporal, mantendo apenas o assunto pesquisado — isso é usado "
    "depois para uma busca por similaridade semântica, então não pode conter "
    "expressões de tempo. Responda SEMPRE em português do Brasil. Responda "
    "APENAS com um JSON válido no formato "
    '{{"cleaned_query": "...", "start_date": "AAAA-MM-DD" ou null, '
    '"end_date": "AAAA-MM-DD" ou null}}, sem markdown e sem texto adicional.'
)


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


async def extract_date_intent(raw_query: str) -> DateIntent:
    """Pergunta à cascata de LLM se a busca tem um filtro de data embutido.

    Em qualquer falha, devolve a busca original sem filtro de data (`
    DateIntent(cleaned_query=raw_query)`) em vez de propagar a exceção —
    ver docstring do módulo.
    """
    if not LLM_CASCADE:
        logger.warning("Nenhum modelo de IA configurado — busca seguirá sem filtro de data")
        return DateIntent(cleaned_query=raw_query)

    today = datetime.now(DISPLAY_TIMEZONE).date().isoformat()
    primary, *fallbacks = LLM_CASCADE
    primary_kwargs = {k: v for k, v in primary.items() if k != "model"}

    try:
        response = await litellm.acompletion(
            model=primary["model"],
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT_TEMPLATE.format(today=today)},
                {"role": "user", "content": raw_query},
            ],
            temperature=0.0,
            timeout=INTENT_LLM_TIMEOUT_SECONDS,
            fallbacks=fallbacks or None,
            **primary_kwargs,
        )
        raw = response.choices[0].message.content or ""
        data = json.loads(extract_json_block(raw))

        cleaned_query = data.get("cleaned_query")
        if not isinstance(cleaned_query, str) or not cleaned_query.strip():
            cleaned_query = raw_query

        return DateIntent(
            cleaned_query=cleaned_query.strip(),
            start_date=_parse_date(data.get("start_date")),
            end_date=_parse_date(data.get("end_date")),
        )
    except Exception:
        logger.exception(
            "Extração de filtro de data falhou para a busca %r — seguindo sem "
            "filtro de data (busca semântica pura)", raw_query,
        )
        return DateIntent(cleaned_query=raw_query)
