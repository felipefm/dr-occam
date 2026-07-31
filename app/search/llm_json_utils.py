"""Extração de um bloco JSON de uma resposta de LLM em texto livre.

Compartilhado por intent_service.py e suggestion_service.py — os dois
pedem uma resposta em JSON estrito, mas modelos (sobretudo locais) às vezes
envolvem a resposta em blocos de markdown ou texto extra ao redor.
"""


def extract_json_block(raw: str) -> str:
    """Extrai estritamente o conteúdo entre a primeira '{' e a última '}',
    descartando blocos de markdown ou qualquer texto ao redor do JSON."""
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Nenhum objeto JSON encontrado na resposta da IA")
    return raw[start : end + 1]
