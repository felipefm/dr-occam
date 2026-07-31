"""Busca híbrida sobre os artigos: filtro de data extraído por IA a partir
de linguagem natural + similaridade semântica (cosseno) via sqlite-vec.

Submódulos:
    schemas: modelos Pydantic de request/response e do resultado
        intermediário da extração de intenção de data.
    intent_service: extrai (via LLM) um possível filtro de data embutido
        na busca em linguagem natural.
    repository: acesso a dados — filtro de data em `article` e KNN por
        cosseno em `vec_articles`. Nenhuma lógica de IA mora aqui.
    service: orquestra intent_service + embeddings + repository e monta a
        resposta final com o percentual de relevância.
    router: rota HTTP `GET /api/search`.
"""
