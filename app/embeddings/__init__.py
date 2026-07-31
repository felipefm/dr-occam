"""Infraestrutura de embeddings semânticos dos artigos (busca por contexto).

Submódulos:
    config: variáveis de ambiente do modelo de embedding e dos lotes.
    repository: acesso a dados (tabela virtual vec0 do sqlite-vec + tabela
        relacional de proveniência). Nenhuma lógica de IA mora aqui.
    service: geração do vetor via LiteLLM. Nenhum acesso a banco mora aqui.
    backfill: script que orquestra repository + service para popular os
        embeddings dos artigos já existentes.
"""
