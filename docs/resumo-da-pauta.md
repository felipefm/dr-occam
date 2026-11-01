# Resumo por IA da pauta

**Status:** implementado na branch `resumo-pauta` (2026-11-01), a partir da POC de resumo por período.

## O que faz

Na tela inicial, o painel da pauta ganhou o quadro **Resumo por IA**. Você escolhe o formato e gera um resumo das notícias da pauta. Dá para gerar várias versões da mesma pauta, nos dois formatos, navegar entre elas (‹ ›) e escolher qual vai no envio e no PDF.

- **Formatos** (`app/summary/modes.py`): *Briefing por temas* (visão geral e depois um parágrafo por tema, padrão) e *Texto corrido*. Para criar outro formato, basta acrescentar uma entrada em `MODES` e o valor em `Mode` (`pauta_router.py`).
- **Entrada do prompt:** o texto original de cada notícia (`original_content`), limitado a `PAUTA_SUMMARY_MAX_CHARS_PER_ARTICLE` (3000) por notícia e `PAUTA_SUMMARY_MAX_TOTAL_CHARS` (60000) no total. Numa pauta arquivada, usa o artigo se ele ainda estiver no banco; senão, o resumo copiado no arquivo.
- **Versão escolhida:** a primeira versão gerada já fica escolhida; as seguintes só passam a valer depois de "Usar esta versão". Se a versão escolhida for apagada, a mais nova que sobrar assume.
- **Envio (Telegram/WhatsApp/Copiar) e PDF:** o resumo escolhido vai no topo, seguido da lista de notícias com os links.
- **Pauta mudou:** se notícias entram ou saem depois de gerar uma versão, o quadro avisa que ela está desatualizada (pela comparação de `article_ids`).
- **Arquivar** leva as versões junto para o arquivo. **Descartar sem arquivar** apaga as versões da pauta atual.
- **Pautas arquivadas:** a página de cada pauta (`/pautas/<id>`) tem o mesmo quadro, então dá para gerar resumo até de pautas arquivadas antes desta funcionalidade. A lista `/pautas` marca "com resumo".

## Arquitetura

```
app/summary/
  engine.py           motor compartilhado: cascata de LLM, timeout, log, trava de 1 resumo por vez
  service.py          resumo por período (POC) → engine
  modes.py            formatos da pauta (rótulo + prompt)
  pauta_service.py    monta a entrada da pauta (atual/arquivada) → engine → grava a versão
  pauta_repository.py tabela pauta_summary
  pauta_router.py     /api/pauta/summaries, /api/pautas/{id}/summaries, /api/pauta-summaries/{id}/choose|DELETE
app/templates/_pauta_summary.html   quadro visual, incluído em index.html e pauta_print.html
```

- Tabela nova `pauta_summary` (criada pelo `create_all`, sem migração): `archive_id` NULL = pauta atual.
- **Um resumo por vez** no servidor (`asyncio.Lock` em `engine.py`), valendo para pauta e período: um segundo pedido recebe 409 na hora. O motivo é o LLM local compartilhado e o histórico de sobrecarga do host.
- A sessão do banco não fica aberta durante a chamada ao LLM.

## Medições (cópia do banco, LM Studio gemma-4-e4b, 2026-11-01)

| Caso | Notícias | Prompt tokens | Tempo |
|---|---|---|---|
| Pauta atual, briefing | 14 | 8.373 | 46,7s |
| Pauta atual, texto corrido | 14 | 8.306 | 28,3s |
| Pauta arquivada, briefing | 2 | 1.644 | 15,7s |
| Pauta atual, briefing | 3 | — | ~24s |

## Achado sobre a POC por período

Em 2026-11-01 o dia já tinha **356** notícias, cerca de 62 mil tokens. O gemma-4-e4b está carregado no LM Studio com contexto de **51.200** tokens, então `/api/summary?days=1` responde 503 (o LM Studio devolve 400). O teto atual, `SUMMARY_MAX_ARTICLES=400`, não protege contra isso. Com `SUMMARY_MAX_ARTICLES=150` funcionou: ~25 mil tokens, 108s. A próxima evolução da POC deve limitar o prompt por tokens/caracteres, não por número de artigos, ou partir para o map-reduce.
