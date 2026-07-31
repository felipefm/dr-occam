"""Orquestração do pipeline completo: ingestão + processamento LLM.

Ponto de entrada único, reaproveitado tanto pelo disparo manual
(`POST /trigger-pipeline` em main.py) quanto pelo agendador interno
(scheduler.py) — a lógica de captura/processamento em si não muda,
só passa a ter um único lugar que a aciona."""

import logging
import traceback

import pipeline_state
from ingestion import run_ingestion
from llm_processor import run_llm_processing

logger = logging.getLogger(__name__)


async def run_pipeline() -> None:
    if not pipeline_state.try_acquire():
        logger.warning(
            "Pipeline: já existe uma execução em andamento — disparo ignorado "
            "para não sobrecarregar o host com execuções concorrentes"
        )
        return

    logger.info("Pipeline: gatilho recebido, iniciando execução em background")
    try:
        logger.info("Pipeline: iniciando etapa de ingestão")
        ingestion_summary = await run_ingestion()
        logger.info("Pipeline: ingestão concluída: %s", ingestion_summary)

        logger.info("Pipeline: iniciando etapa de processamento LLM")
        llm_summary = await run_llm_processing()
        logger.info("Pipeline: processamento LLM concluído: %s", llm_summary)

        logger.info("Pipeline: execução finalizada com sucesso")
    except Exception as e:
        logger.error("Pipeline: falha durante a execução: %s", e)
        traceback.print_exc()
    finally:
        pipeline_state.release()
