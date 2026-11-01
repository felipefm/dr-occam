# Plano de implementação — POC: Resumo de notícias por período

**Status:** plano pronto para execução, nenhum código escrito ainda.
**Depende de:** `poc-resumo-por-periodo.md` (análise de viabilidade e decisões de escopo, seção 8 já respondida em 2026-08-13). Este documento não repete a análise — só traduz as decisões em passos de implementação.

**Decisões já tomadas (resumo, ver seção 8 do doc de origem):**

1. Janela **configurável**: hoje / últimos 2 dias / últimos 3 dias (sem opção livre).
2. UI como **nova aba/seção dentro de `/search`** (mesma página, não uma rota nova).
3. Tempo de resposta **medido durante a implementação** (via log), teto ajustado ao final da POC.
4. Resumo **inclui lista de fontes** (título + link) no rodapé.

---

## 1. Semântica da janela

Para eliminar ambiguidade antes de codar, a janela é definida como:

| Opção na UI | `days` | `start_date` | `end_date` |
|---|---|---|---|
| Hoje | 1 | hoje | hoje |
| Últimos 2 dias | 2 | hoje − 1 | hoje |
| Últimos 3 dias | 3 | hoje − 2 | hoje |

Ou seja: `start_date = hoje − (days − 1)`, `end_date = hoje`, ambos no fuso de exibição (`America/Sao_Paulo`, já centralizado em `timezone_utils.DISPLAY_TIMEZONE`). Mesmo critério textual usado em `search/intent_service.py` para "últimos N dias", adaptado para não deixar dúvida entre N e N−1.

`days` é restrito a `{1, 2, 3}` — validado no endpoint (`Query(..., ge=1, le=3)` mais checagem explícita, já que não é um range contínuo conceitualmente, é um de três valores fixos).

## 2. Módulo novo: `app/summary/`

Mesma organização de `app/search/` (router fino → service orquestra → repository só lê dados, sem lógica de IA):

```
app/summary/
  __init__.py
  config.py       # timeouts e tetos, no padrão de search/config.py
  schemas.py      # SummaryResponse, SummarySourceItem
  repository.py   # busca de artigos da janela (reaproveita search.repository)
  service.py      # monta prompt, chama LLM, mede tempo, monta resposta
  router.py       # GET /api/summary
```

Registrado em `main.py` junto de `search_router`:
```python
from summary.router import router as summary_router
...
app.include_router(summary_router)
```

### 2.1 `summary/config.py`

```python
SUMMARY_LLM_TIMEOUT_SECONDS = int(os.getenv("SUMMARY_LLM_TIMEOUT_SECONDS", "240"))
SUMMARY_MAX_ARTICLES = int(os.getenv("SUMMARY_MAX_ARTICLES", "400"))
```

- `SUMMARY_LLM_TIMEOUT_SECONDS`: começa igual ao `LLM_TIMEOUT_SECONDS`/`INTENT_LLM_TIMEOUT_SECONDS` atual (240s, já configurado para o modelo local em `.env`), mas como variável própria — o prompt de resumo é ordens de magnitude maior que os prompts de título/resumo por artigo, então pode acabar precisando de um valor diferente. Ajustar com base na medição da seção 5.
- `SUMMARY_MAX_ARTICLES`: teto rígido de artigos concatenados no prompt, **além** do teto de dias. Existe por segurança de host: o volume por dia pode crescer (mais fontes cadastradas) sem que o teto de dias mude — isso protege contra o mesmo tipo de incidente descrito na seção 4 do doc de origem (sobrecarga do host por chamada de IA descontrolada). Se o número de artigos da janela exceder o teto, usa os `SUMMARY_MAX_ARTICLES` mais recentes e sinaliza isso na resposta (`truncated: true`). Não estava nas perguntas em aberto, mas é uma consequência direta do risco já documentado — sinalizando aqui para você confirmar se quer essa guarda na v1 ou prefere adiar.

### 2.2 `summary/schemas.py`

```python
class SummarySourceItem(BaseModel):
    title: str
    url: str
    published_at: datetime

class SummaryResponse(BaseModel):
    window_days: int
    start_date: date
    end_date: date
    article_count: int
    truncated: bool  # True se article_count > SUMMARY_MAX_ARTICLES
    summary_text: str
    sources: list[SummarySourceItem]
    pipeline_running: bool  # ver seção 4
```

Sem `results: list` de cards — é texto corrido, por decisão de escopo do doc de origem (seção 2).

### 2.3 `summary/repository.py`

Reaproveita `search.repository.get_candidate_article_ids` diretamente (mesma filtragem por `status == PROCESSED` + janela de data — é exatamente o que a seção 2 do doc de origem recomenda reaproveitar). Acrescenta só a função que falta:

```python
def get_articles_for_summary(
    session: Session, candidate_ids: set[int], max_articles: int
) -> tuple[list[Article], bool]:
    """Retorna os artigos da janela em ordem cronológica (mais antigo
    primeiro, para o LLM ler na ordem dos fatos), até max_articles.
    Segundo valor indica se houve corte (mais candidatos que o teto)."""
```

Sem chamada a embedding/KNN — só `SELECT ... WHERE id IN (...) ORDER BY created_at ASC LIMIT max_articles+1` (o `+1` só para saber se truncou, sem precisar de um `COUNT` separado).

### 2.4 `summary/service.py`

Fluxo:

1. Calcula `start_date`/`end_date` a partir de `days` (seção 1).
2. Converte para UTC naive via helper compartilhado (ver seção 3 — pequeno refactor necessário).
3. `get_candidate_article_ids` → se vazio, retorna `SummaryResponse` com `article_count=0`, `summary_text=""` e uma mensagem tratada no front (não é erro, mesmo padrão de busca vazia em `search/service.py`).
4. `get_articles_for_summary` → monta o prompt.
5. Chama a cascata de LLM (`llm_cascade.LLM_CASCADE`, mesmo padrão de `intent_service.py`/`llm_processor.py`: `primary, *fallbacks = LLM_CASCADE`, `litellm.acompletion(..., fallbacks=fallbacks, timeout=SUMMARY_LLM_TIMEOUT_SECONDS)`).
6. Mede e loga tempo de execução + tokens (ver seção 5).
7. Monta `sources` a partir dos **próprios artigos já buscados no banco** (título + `original_url` + `created_at`) — nunca a partir do texto que o LLM devolve. Isso evita depender do modelo para não inventar/alucinar URLs, e é mais barato (não pede pro LLM repetir dado que já temos).
8. `pipeline_running = pipeline_state.is_running()` — um booleano de leitura simples (a função já existe, é só chamar), para o front-end avisar o usuário sem precisar de lógica nova de trava (ver seção 4).

Erro de LLM (timeout, cascata inteira fora do ar) → exceção própria `SummaryUnavailableError`, traduzida pelo router em HTTP 503, mesmo padrão de `EmbeddingUnavailableError` em `search/service.py`.

**Prompt (rascunho para a v1, ajustável depois de ver resultados reais):**

```
SYSTEM:
Você é um editor de notícias neutro e imparcial do agregador Dr. Occam.
Você recebe uma lista numerada de notícias já resumidas (título e resumo
neutros) publicadas num mesmo período. Produza um texto corrido em
português do Brasil que sintetize os principais assuntos do período, sem
ser uma lista mecânica de manchetes coladas: agrupe naturalmente notícias
sobre o mesmo fato ou tema relacionado, evitando repetição. Seja
estritamente factual, sem adjetivos sensacionalistas, sem opinião e sem
viés político ou ideológico. NÃO invente informações que não estejam nos
resumos fornecidos. NÃO inclua links, URLs ou numeração de fonte no texto
— a lista de fontes é exibida separadamente pelo sistema. Responda apenas
com o texto corrido da síntese, sem markdown, sem títulos de seção, sem
frases de abertura como "Aqui está um resumo:".

USER:
1. [{ai_title}] {ai_summary}
2. [{ai_title}] {ai_summary}
...
```

Sem JSON de saída — ao contrário de `llm_processor.py`/`intent_service.py`, aqui a resposta é o próprio texto final a ser exibido, então não precisa de parsing estruturado (menos um ponto de falha por JSON malformado).

### 2.5 `summary/router.py`

```python
@router.get("", response_model=SummaryResponse, ...)
async def summarize_period(
    days: int = Query(..., description="1, 2 ou 3 dias"),
) -> SummaryResponse:
    if days not in (1, 2, 3):
        raise HTTPException(422, "days deve ser 1, 2 ou 3")
    with Session(engine) as session:
        try:
            return await build_period_summary(session, days)
        except SummaryUnavailableError as exc:
            raise HTTPException(503, str(exc)) from exc
```

Prefixo `/api/summary`, tag "Resumo" (nova tag em `TAGS_METADATA` no `main.py`, mesmo padrão de "Busca").

## 3. Pequeno refactor necessário: helpers de data/fuso

`search/service.py` tem `_local_date_to_utc_naive` e `_as_aware_utc`, hoje privados (prefixo `_`) e usados só ali. O módulo de resumo precisa exatamente da mesma conversão (data de calendário local → instante UTC naive, para comparar com `Article.created_at`). Em vez de duplicar a lógica (ela tem uma nuance sutil e comentada — ver docstring atual) ou importar um símbolo privado de outro módulo, mover as duas funções para `timezone_utils.py` (que já centraliza `DISPLAY_TIMEZONE`) como funções públicas, e atualizar `search/service.py` para importar de lá. `summary/service.py` importa do mesmo lugar. Escopo pequeno, sem mudança de comportamento.

## 4. Contenção com o pipeline (risco da seção 6 do doc de origem)

Decisão para a v1: **sem trava nova**, só visibilidade. `pipeline_state.is_running()` já existe e é só uma leitura de flag em memória — reaproveitada como campo `pipeline_running` na resposta (seção 2.2). O front-end mostra um aviso complementar se vier `true` ("o pipeline está rodando agora, a resposta pode demorar mais"), reaproveitando o mesmo texto de aviso já usado no "slow hint" da busca. no bloqueio de fato: não implementar — está fora do escopo decidido, e criar uma fila/trava específica para o resumo seria a "unificação"/complexidade que a doc de origem explicitamente adia (seção 5, "fora do escopo").

## 5. Instrumentação de tempo (decisão 3: medir durante a POC)

Sem tabela nova no banco nem dashboard — só log estruturado, aproveitando que `/api/logs` (já usado pelo modal do `/admin`) mostra as últimas linhas em memória. Em `summary/service.py`, ao redor da chamada ao LLM:

```python
started_at = time.monotonic()
response = await litellm.acompletion(...)
elapsed = time.monotonic() - started_at
logger.info(
    "Resumo por período: days=%d artigos=%d truncated=%s prompt_tokens=%d "
    "completion_tokens=%d tempo=%.1fs",
    days, len(articles), truncated, prompt_tokens, completion_tokens, elapsed,
)
```

Isso dá, sem infraestrutura nova, exatamente o dado que falta hoje (seção 6 do doc de origem: "vale medir o tempo real antes de decidir o teto") — só que medido em uso real em vez de um teste isolado. Ao final da POC, revisitar `SUMMARY_LLM_TIMEOUT_SECONDS` e o teto de `days` (hoje travado em 3) com base nesses números.

## 6. Front-end: nova aba em `/search`

`templates/search.html` ganha um seletor de abas no topo (ex.: dois botões "Buscar" / "Resumir por período" alternando `hidden` em dois blocos `<div>` — mesmo estilo Tailwind já usado na página, sem framework novo). Nenhuma mudança em `main.py` na rota `/search` (continua servindo o mesmo template, só que agora com duas seções).

Bloco novo (`#summary-panel`):
- Seletor de janela: 3 botões/radio "Hoje" / "Últimos 2 dias" / "Últimos 3 dias" (não input de texto livre — decisão 1).
- Botão "Gerar resumo".
- Área de status (reaproveita o padrão `setStatus`/slow-hint já existente em `search.html`, com um hint mais longo dado que o prompt é maior — ex. 20s em vez de 15s antes do aviso "IA local pode demorar mais").
- Bloco de texto do resumo (`whitespace-pre-line`, mesmo tratamento usado no resumo de cada card da busca).
- Lista de fontes no rodapé: título + link, reaproveitando o mesmo padrão de link usado no card de busca (`target="_blank" rel="noopener noreferrer"`).
- Se `pipeline_running: true` na resposta, ou uma checagem antes de disparar (a decidir na implementação: mais simples é só ler o campo da própria resposta e avisar depois, já que não há endpoint de status do pipeline exposto separadamente hoje).

JS: função `runSummary(days)` no mesmo IIFE de `search.html` (ou extraído para um `<script>` próprio, se o arquivo ficar grande demais) — mesmo padrão de `fetch` + `try/finally` + `setLoading` já usado em `runSearch`.

## 7. Ordem de implementação sugerida

1. Refactor dos helpers de data para `timezone_utils.py` (seção 3) — isolado, sem risco, testável com a busca existente continuando a funcionar igual.
2. `summary/` backend completo (config → schemas → repository → service → router), registrado em `main.py`.
3. Testar o endpoint isolado (`curl localhost:8383/api/summary?days=1`) e observar o log de tempo (seção 5) antes de mexer em front-end — é o ponto onde se descobre se `SUMMARY_LLM_TIMEOUT_SECONDS=240` é suficiente ou se `days=1` (192 artigos, ~35k tokens conforme medição da seção 3 do doc de origem) já estoura.
4. Front-end (aba nova em `search.html`).
5. Teste manual ponta a ponta para os 3 valores de `days`, observando os critérios de sucesso da seção 7 do doc de origem (tempo aceitável, resumo coerente, nenhum impacto no resto do sistema).

## 8. Fora de escopo (reafirmando o que o doc de origem já excluiu)

Nada de map-reduce, clustering por tópico, cache de resumo gerado, unificação com o campo de busca livre, ou filtro por assunto dentro do período — permanece tudo como evolução futura, não bloqueante (seção 5 do doc de origem).

## 9. Resultados da implementação (2026-08-13)

Implementado na branch `poc-resumo-por-periodo`: refactor dos helpers de data para `timezone_utils.py`, módulo `app/summary/` completo, registro em `main.py`, aba nova em `templates/search.html`, variáveis `SUMMARY_LLM_TIMEOUT_SECONDS`/`SUMMARY_MAX_ARTICLES` no `.env`.

**Testes de backend (via `curl`, container `dr_occam_api` reiniciado para carregar o código):**

| `days` | Janela | Artigos | Prompt tokens | Tempo |
|---|---|---|---|---|
| 1 | hoje | 192 | 31.855 | 167,9s |
| 2 | 12/08–13/08 | 192 | 31.855 | 58,7s |
| 3 | 11/08–13/08 | 192 | 31.855 | 52,3s |

**Achado importante:** os três testes retornaram exatamente os mesmos 192 artigos — não há artigos `PROCESSED` de 11/08 nem 12/08 no banco ainda (a base é recente, ver seção 3 do doc de origem). Ou seja, **ainda não foi possível medir o tempo real de um prompt genuinamente maior** (2-3 dias de artigos de verdade) — só foi possível confirmar que o mesmo prompt (~192 artigos, ~32k tokens) roda de forma confiável dentro do timeout de 240s, com variação grande de tempo de resposta (52s a 168s) para conteúdo idêntico — indício de que o tempo depende bastante da carga momentânea do LM Studio compartilhado, não só do tamanho do prompt.

**Qualidade:** nos três casos o texto saiu coerente, em português, organizado por tema (não uma lista de manchetes coladas), sem links inventados (fontes vêm do banco, não do LLM).

**Pendente:** validar `days=2`/`3` com volume de artigos realmente maior — só será possível numa sessão futura, depois que a base acumular mais dias de histórico. Até lá, o teto de 3 dias é uma aposta razoável (dado que 1 dia já usa ~70% do timeout no pior caso observado), mas não totalmente validado.

**Validação de UI:** implementada (aba "Resumir por período" em `/search`, reaproveitando os padrões visuais e de JS já existentes), mas **não testada em navegador nesta sessão** — a extensão Claude in Chrome não estava configurada no ambiente. Precisa de um clique manual de ponta a ponta antes de considerar a POC encerrada (ver seção 7 do doc de origem, critério de sucesso).

## 10. Atualização (2026-11-01)

- A chamada ao LLM foi extraída para `app/summary/engine.py`, compartilhada com o resumo da pauta (ver `docs/resumo-da-pauta.md`). Agora um segundo pedido simultâneo recebe 409 em vez de rodar em paralelo.
- Com 356 notícias no dia (~62 mil tokens), `days=1` estoura o contexto de 51.200 tokens do gemma-4-e4b e responde 503. `SUMMARY_MAX_ARTICLES=400` é folgado demais; com 150 funcionou em 108s. Detalhes em `docs/resumo-da-pauta.md`.
