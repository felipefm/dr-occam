"""Configuração de ambiente para geração e indexação de embeddings.

Variáveis de ambiente:
    EMBEDDING_MODEL: nome do modelo de embedding no formato LiteLLM
        (default "text-embedding-3-small").
    EMBEDDING_API_KEY: chave da API do provedor de embeddings. Se ausente,
        cai para OPENAI_API_KEY (mesma variável padrão que o LiteLLM já
        reconhece nativamente para modelos "openai/...").
    EMBEDDING_API_BASE: URL customizada do provedor (ex.: proxy LiteLLM,
        endpoint local compatível com a API da OpenAI). Opcional.
    EMBEDDING_DIMENSIONS: dimensão do vetor. Também define o tamanho fixo
        da coluna da tabela virtual vec0 — mudar este valor depois de criar
        a tabela exige recriá-la (ver embeddings/repository.py).
    EMBEDDING_BATCH_SIZE: quantidade de artigos processados por lote no
        backfill (default 50).
    EMBEDDING_MAX_CONCURRENCY: chamadas concorrentes ao provedor de
        embeddings (default 5).
    EMBEDDING_TIMEOUT_SECONDS: timeout por chamada de embedding, em segundos
        (default 60) — sem isso, uma chamada que trava (glitch de rede,
        provedor local reiniciando/recarregando o modelo etc.) prende o
        backfill inteiro indefinidamente, sem cair no tratamento de erro
        por artigo que já existe em embeddings/backfill.py.
    EMBEDDING_DOCUMENT_PREFIX / EMBEDDING_QUERY_PREFIX: prefixo de tarefa
        prependido ao texto antes de embedar, vazio por default. Alguns
        modelos (notavelmente a família nomic-embed-text) exigem prefixos
        como "search_document: " / "search_query: " para discriminar bem
        entre embeddings de documento e de busca — sem isso o modelo não dá
        erro, só produz vetores mal discriminados (similaridade artificial-
        mente parecida entre artigos sem relação nenhuma). Provedores que
        não usam essa convenção (ex.: OpenAI) devem deixar ambos vazios.
"""

import os

EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_API_KEY: str | None = os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY")
EMBEDDING_API_BASE: str | None = os.getenv("EMBEDDING_API_BASE")
EMBEDDING_DIMENSIONS: int = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))
EMBEDDING_BATCH_SIZE: int = int(os.getenv("EMBEDDING_BATCH_SIZE", "50"))
EMBEDDING_MAX_CONCURRENCY: int = int(os.getenv("EMBEDDING_MAX_CONCURRENCY", "5"))
EMBEDDING_TIMEOUT_SECONDS: int = int(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "60"))
EMBEDDING_DOCUMENT_PREFIX: str = os.getenv("EMBEDDING_DOCUMENT_PREFIX", "")
EMBEDDING_QUERY_PREFIX: str = os.getenv("EMBEDDING_QUERY_PREFIX", "")
