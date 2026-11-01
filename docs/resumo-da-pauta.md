# Resumo por IA da pauta

**Status:** implementado na branch `resumo-pauta` (2026-11-01). Substitui a POC de resumo por período, abandonada (ver `docs/poc-resumo-por-periodo.md`).

## O que faz

Na tela inicial, o painel da pauta ganhou o quadro **Resumo por IA**. Você escolhe o formato e gera um resumo das notícias da pauta. Dá para gerar várias versões da mesma pauta, nos dois formatos, navegar entre elas (‹ ›) e escolher qual vai no envio e no PDF.

- **Formatos editáveis no `/admin`** (seção "Formatos de resumo da pauta", link direto `/admin#formatos`): nome, prompt, ordem no seletor e ativo/inativo. Dá para criar formatos novos e apagá-los. Os de fábrica, *Briefing por temas* e *Panorama*, só podem ser desativados e têm "Restaurar padrão". O conteúdo de fábrica fica em `app/summary/formats.py` (`DEFAULT_FORMATS`).
- **Regras fixas do sistema** (`FIXED_RULES` em `formats.py`): anexadas pelo código ao fim de todo prompt, fora do alcance do `/admin`. São elas: sem links/URLs/numeração (o sistema anexa os links) e texto simples, sem markdown. Como reforço, `clean_output` tira asteriscos e `#` que o modelo devolva mesmo assim.
- **Cada versão guarda o nome e o prompt usados** (`pauta_summary.mode_label` / `prompt`). O quadro mostra "Ver o prompt usado nesta versão" e avisa quando o prompt do formato foi editado depois daquela versão, o que permite comparar o antes e o depois de uma edição.
- **Entrada do prompt:** o texto original de cada notícia (`original_content`), limitado a `PAUTA_SUMMARY_MAX_CHARS_PER_ARTICLE` (3000) por notícia e `PAUTA_SUMMARY_MAX_TOTAL_CHARS` (60000) no total. Numa pauta arquivada, usa o artigo se ele ainda estiver no banco; senão, o resumo copiado no arquivo.
- **Versão escolhida:** a primeira versão gerada já fica escolhida; as seguintes só passam a valer depois de "Usar esta versão". Se a versão escolhida for apagada, a mais nova que sobrar assume.
- **Envio (Telegram/WhatsApp/Copiar) e PDF:** o resumo escolhido vai no topo, seguido da lista de notícias com os links.
- **Pauta mudou:** se notícias entram ou saem depois de gerar uma versão, o quadro avisa que ela está desatualizada (pela comparação de `article_ids`).
- **Arquivar** leva as versões junto para o arquivo. **Descartar sem arquivar** apaga as versões da pauta atual.
- **Pautas arquivadas:** a página de cada pauta (`/pautas/<id>`) tem o mesmo quadro, então dá para gerar resumo até de pautas arquivadas antes desta funcionalidade. A lista `/pautas` marca "com resumo".

## Cobertura: garantir todas as notícias

O modelo local esquece notícias em pautas grandes. No exemplo de 01/11, o Panorama citou 13 de 24. Por isso, nos formatos com **"Garantir todas as notícias"** ligado (opção por formato no `/admin`, ligada por padrão), a geração tem duas rodadas:

1. **1ª rodada:** o resumo normal.
2. **Conferência** (`app/summary/coverage.py`), sem usar o modelo de texto: compara, por embedding, cada notícia (título + resumo do pipeline) com cada frase do resumo e fica com a frase mais parecida.
   - semelhança ≥ `PAUTA_COVERAGE_HIGH` (0,80): citada;
   - < `PAUTA_COVERAGE_LOW` (0,74): faltando;
   - no meio: citada só se a frase mais parecida tiver ao menos metade dos nomes próprios do título. Na dúvida, conta como faltando: o pior caso é uma notícia citada duas vezes.
3. **2ª rodada (só se faltar alguma):** a IA recebe apenas as notícias que faltaram e devolve uma linha curta por notícia. O **código** monta "Também na pauta: …; …" e junta ao fim do texto, aproveitando o "Também na pauta:" que já existir. Notícia sem linha na resposta entra pelo título, então a cobertura é garantida pelo código.

Cada versão guarda `coverage` (`{"total", "completed": [{"n", "title"}]}`), e o quadro mostra "Cobertura: X de N no texto + Y completadas (ver quais)". Se o provedor de embeddings falhar, a versão sai só com a 1ª rodada e `coverage` NULL.

**Calibração (2026-11-01, nomic-embed-text-v1.5, pauta de 24 notícias, conferida à mão):**

| Texto da 1ª rodada | Faltavam | Detectou | Deixou passar | Falso alarme |
|---|---|---|---|---|
| Panorama antigo (o do PDF) | 11 | as 11 | 0 | 0 |
| Panorama novo | 5, 7 | 5, 7 | 0 | 0 |
| Briefing | 5, 11 | 2, 5, 11 | 0 | 1 (nº 2, citada sem nenhum nome) |
| Panorama, pauta de 7 notícias | nenhuma | nenhuma | 0 | 0 |

A 2ª rodada levou de 2 a 6 segundos.

## Arquitetura

```
app/summary/
  engine.py           motor: cascata de LLM, timeout, log, trava de 1 resumo por vez
  formats.py          formatos de fábrica, regras fixas, limpeza de markdown
  coverage.py         conferência de cobertura e montagem do "Também na pauta:"
  format_repository.py / format_router.py   tabela summary_format e API do /admin (/api/summary-formats)
  pauta_service.py    monta a entrada da pauta (atual/arquivada) → engine → grava a versão
  pauta_repository.py tabela pauta_summary
  pauta_router.py     /api/pauta/summaries, /api/pautas/{id}/summaries, /api/pauta-summaries/{id}/choose|DELETE
app/templates/_pauta_summary.html   quadro visual, incluído em index.html e pauta_print.html
```

- Tabela `pauta_summary`: `archive_id` NULL = pauta atual. As colunas `mode_label` e `prompt` entraram por migração (`database.py`); versões antigas recebem o nome pelo `LEGACY_LABELS`.
- Tabela `summary_format`: os formatos de fábrica que faltarem são criados ao subir a aplicação (`ensure_default_formats`), sem sobrescrever edições.
- **Um resumo por vez** no servidor (`engine.exclusive()`, um `asyncio.Lock` que cobre as duas rodadas): um segundo pedido recebe 409 na hora.
- Prompts de fábrica que mudam: o texto antigo vai para `PREVIOUS_DEFAULT_PROMPTS` (`formats.py`); ao subir, um formato que ainda tem o prompt de fábrica antigo, sem edição, é atualizado sozinho. Um formato editado nunca é tocado. O motivo é o LLM local compartilhado e o histórico de sobrecarga do host.
- A sessão do banco não fica aberta durante a chamada ao LLM.

## Medições (cópia do banco, LM Studio gemma-4-e4b, 2026-11-01)

| Caso | Notícias | Prompt tokens | Tempo |
|---|---|---|---|
| Pauta atual, briefing | 14 | 8.373 | 46,7s |
| Pauta atual, texto corrido (formato antigo, substituído pelo Panorama) | 14 | 8.306 | 28,3s |
| Pauta atual, panorama | 24 | 13.227 | 59,7s |
| Pauta arquivada, briefing | 2 | 1.644 | 15,7s |
| Pauta atual, briefing | 3 | — | ~24s |

## Por que não resumir o período inteiro

A POC de resumo por período foi abandonada ao fazer esta funcionalidade. Em 2026-11-01 o dia já tinha 356 notícias, cerca de 62 mil tokens, acima do contexto de 51.200 tokens do modelo local. A pauta resolve a mesma necessidade com um volume pequeno e previsível. Detalhes em `docs/poc-resumo-por-periodo.md`.
