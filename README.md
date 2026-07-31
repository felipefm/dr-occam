# Dr. Occam

Agregador de notícias neutro. Um sistema autônomo de curadoria que lê feeds de fontes globais, extrai o texto principal das notícias, remove viés ideológico e ruído editorial usando IA, e entrega um resumo factual e conciso — consumível tanto por um painel web simples quanto por qualquer leitor RSS.

## O problema

Acompanhar notícias exige consumir múltiplas fontes, e a maior parte do texto vem carregada de viés e ruído jornalístico. O usuário gasta tempo demais filtrando isso manualmente. O Dr. Occam automatiza essa triagem, com controle total de custo e infraestrutura (uso pessoal, rodando em container, com processamento de IA plugável).

## Arquitetura

O `docker-compose.yml` sobe dois containers:

- **`api-occam`** (`./app`, FastAPI + SQLModel/SQLite): o núcleo do Dr. Occam — orquestra ingestão, processamento por IA e expõe o painel e o feed RSS. A extração de conteúdo das notícias roda in-process via `trafilatura`, sem depender de nenhum microsserviço externo de scraping/renderização.
- **`rsshub`** (imagem `diygod/rsshub`, porta `1200`): instância própria do [RSSHub](https://github.com/DIYgod/RSSHub), projeto open-source que gera feeds RSS para sites que não oferecem um nativamente. Não é consumido automaticamente pelo `api-occam` — serve pra gerar manualmente a URL de um feed (ex.: `http://rsshub:1200/bbc/technology` pra `bbc.com/technology`) que depois é cadastrada como uma fonte `RSS` comum no `/admin`. Ver "Ideia futura: auto-detecção de rota RSSHub" no Roadmap abaixo.

### Pipeline de dados

1. **Ingestão** (`app/ingestion.py`): lê as fontes `active=True` da tabela `source`. Para fontes `RSS`, extrai os links via `feedparser` (junto com o texto de `<content:encoded>`/`<description>` de cada item, guardado como fallback). Aplica o filtro anti-ruído de `NEGATIVE_KEYWORDS` (título/URL) antes de gastar uma extração, deduplica contra URLs já salvas (por status, qualquer artigo já coletado antes nunca é recoletado) e corta o restante em até `max_daily_articles` artigos inéditos **por execução** — não é um teto diário rígido: se essa quantidade não cobrir tudo que é novo no feed, o excedente simplesmente fica pra próxima execução (ou é substituído por notícias mais novas), sem nenhum descarte/arquivamento automático. Para cada link novo aprovado, baixa a página com o `httpx.AsyncClient` compartilhado (headers de navegador, retry com backoff exponencial em falhas transitórias como 503) e extrai o texto principal com `trafilatura.extract`; se a página não puder ser baixada/raspada (paywall, bloqueio anti-bot, 401 etc.), cai para o texto já entregue no próprio feed, se ele tiver tamanho mínimo plausível de artigo. O artigo resultante é salvo como `PENDING`. Os artigos de uma mesma fonte são processados **estritamente em sequência** (um por vez, com uma pausa de 2s entre eles) para manter o uso de CPU/RAM baixo.
2. **Deduplicação** *(planejado, ainda não implementado — ver Roadmap)*: agruparia notícias do mesmo evento vindas de fontes diferentes (`cluster_id`) antes do processamento por IA, para não gastar tokens processando o mesmo fato várias vezes.
3. **Processamento por IA** (`app/llm_processor.py`): processa artigos `PENDING` em lotes sucessivos de até `LLM_BATCH_SIZE` (padrão 20), repetindo os lotes dentro da mesma execução até a fila `PENDING` esvaziar — um único disparo do pipeline dá conta de todo o backlog acumulado, não só do primeiro lote. Circuito de segurança: se um lote inteiro não gerar nenhum `PROCESSED` nem `DEAD` (ex.: a IA está fora do ar), a execução para nos lotes seguintes em vez de repetir os mesmos artigos em loop apertado — o restante segue `PENDING` para o próximo disparo. Para cada artigo, trunca o conteúdo em `MAX_CONTENT_LENGTH` caracteres (marcando `is_truncated`) e envia para uma **cascata de modelos** (via `litellm`, ver seção abaixo), pedindo um título e resumo neutros e factuais em JSON. O prompt instrui o modelo a nunca usar aspas duplas dentro de `title`/`summary`; mesmo assim, o parsing da resposta é resiliente: extrai estritamente o bloco entre `{` e `}` (descarta markdown/texto ao redor) e, se o `json.loads()` falhar (tipicamente por aspas internas não escapadas), tenta um fallback por regex específico pro formato `{"title": ..., "summary": ...}` antes de desistir — se nem isso reconhecer o texto, o log de warning inclui a resposta bruta da IA para depuração. Cada tentativa é registrada em `llm_processing_log` (modelo que efetivamente respondeu, versão do prompt, tokens, status). Sucesso → `PROCESSED`; falha em toda a cascata (ou JSON irrecuperável) → soma em `retry_count`, e após `LLM_MAX_RETRIES` tentativas (padrão 5) o artigo vira `DEAD`. Um timeout na cascata inteira ganha uma segunda chance automática no fim do mesmo ciclo (fila `standby_queue` em memória, ver docstring do módulo); se essa repescagem também der timeout, vai direto para `DEAD` sem esperar as demais tentativas. Artigos `DEAD` não são retentados sozinhos — só voltam a `PENDING` sob demanda via `POST /pipeline/reprocess-dead` (ou o botão "☠ Reprocessar DEAD" no `/admin`), que também zera `retry_count`.
4. **Consumo**: `GET /` lista os artigos `PROCESSED` mais recentes em HTML; `GET /feed.xml` gera o mesmo conteúdo como RSS 2.0 válido, pronto para qualquer leitor de feeds; `GET /admin` é o painel de gestão das fontes.

### Painel de administração (`/admin`)

Template Jinja2 (`app/templates/admin.html`) servido pelo próprio FastAPI, sem build step — Tailwind CSS via CDN (`cdn.tailwindcss.com`) para o estilo e JavaScript puro (`fetch`) para o CRUD, sem nenhum framework frontend. A lista inicial é renderizada no servidor; toda ação (adicionar, alternar status, editar limite, excluir) chama a API REST correspondente e atualiza só a linha afetada no DOM, sem recarregar a página. Fontes são sempre criadas como `RSS` (o formulário só pede nome + URL); o campo `source_type` fica de fora do formulário porque a extração de links só é implementada para `RSS`.

Layout responsivo: abaixo de 640px a tabela de fontes vira uma lista de cards empilhados (cada `<td>` reaparece com um rótulo, via `data-label` + CSS), e o menu de ações do topo colapsa atrás de um botão hambúrguer — sem overflow horizontal indesejado nem botões cortados. O link **"← Voltar para o feed"** fica fora desse menu de ações, como link de navegação persistente sempre visível acima do título (não depende do hambúrguer nem compete visualmente com os botões de ação).

O `/admin` também tem um botão **"Executar varredura"** (dispara `POST /trigger-pipeline`), um botão **"☠ Reprocessar DEAD"** (dispara `POST /pipeline/reprocess-dead`, ressuscitando até 20 artigos `DEAD` para `PENDING` e reprocessando-os) e um botão **"Ver logs"** que abre um modal estilo terminal (preto/verde) com as últimas linhas de log da aplicação, atualizando por polling (`GET /api/logs`, a cada 2s) enquanto o modal estiver aberto — dá pra acompanhar o pipeline rodando sem precisar de `docker logs` no terminal. Os logs ficam num buffer em memória (`collections.deque`, últimas 500 linhas, `app/main.py`); reiniciar o container zera o histórico.

A seção **"🕒 Agendamento automático"** (um accordeon `<details>` nativo, recolhido por padrão, com um badge "Ativado"/"Manual" visível mesmo fechado) configura o agendador interno descrito na próxima seção — liga/desliga e lista de horários `HH:MM`, salvos via `PUT /api/schedule`. Detalhe de robustez: alguns navegadores mobile restauram o estado de checkboxes de uma aba suspensa (troca de app/bloqueio de tela) em vez de refletir o HTML atual do servidor; para evitar o badge mostrar "Manual" quando na verdade está ativado (ou vice-versa), a página reconsulta `GET /api/schedule` sempre que volta de um estado suspenso (evento `pageshow` com `persisted: true`).

### Agendador automático (`app/scheduler.py`)

O pipeline pode rodar sozinho, em horários configuráveis, **sem depender do cron do sistema operacional hospedeiro**. Implementado com `APScheduler` (`AsyncIOScheduler`, acoplado ao mesmo event loop do `uvicorn`) — cada horário configurado vira um `CronTrigger` com timezone fixo em `America/Sao_Paulo`, independente do fuso do host/container.

A configuração (`enabled` + lista de horários `times`) é persistida na tabela `schedule_config` (linha única) e recarregada do banco no `lifespan` de startup do FastAPI (`start_scheduler()`) — sobrevive a reinícios do container. É editável em tempo real pelo `/admin` ou via API (`GET`/`PUT /api/schedule`); salvar reconfigura os jobs na hora, sem reiniciar o processo.

Três modos possíveis, todos com o mesmo mecanismo (só muda a quantidade de horários):

- **Manual** (`enabled=false`): pipeline só dispara via `POST /trigger-pipeline` / botão "Executar varredura".
- **Execução única diária** (`enabled=true`, 1 horário): ex. `["06:00"]`.
- **Múltiplas execuções por dia** (`enabled=true`, vários horários): ex. `["08:00", "14:00", "20:00"]`.

Cada job usa `max_instances=1` e `coalesce=True` — nunca duas execuções do mesmo horário sobrepostas, e se o container ficou fora do ar e perdeu disparos, roda só uma vez ao voltar (não empilha reexecuções perdidas). O disparo em si (`run_pipeline()`) mora em `app/pipeline.py`, um único ponto de entrada reaproveitado tanto pelo agendador quanto pelo `POST /trigger-pipeline` manual — a lógica de ingestão/processamento não muda, só passa a ter um único lugar que a aciona.

**Pressuposto importante:** assume um único processo/worker `uvicorn` (é como o `Dockerfile` já sobe a aplicação hoje). Com múltiplos workers, cada um teria seu próprio agendador em memória e o pipeline disparia em duplicidade — se isso mudar no futuro, o agendamento precisa migrar pra um processo dedicado.

### Proteção contra execução concorrente e limites de recursos

`app/pipeline_state.py` guarda um flag simples em memória de processo (mesma premissa de único worker `uvicorn` do agendador acima): `run_pipeline()` e o reprocessamento de `DEAD` (`_run_llm_reprocessing`) só rodam se não houver outra execução em andamento — clicar de novo em "Executar varredura" (ou disparar `POST /pipeline/reprocess-dead`) enquanto uma varredura já está rodando responde `409` com uma mensagem clara, em vez de empilhar execuções concorrentes independentes.

Essa trava existe porque, sem ela, cliques repetidos (ou uma varredura agendada coincidindo com um disparo manual) somavam várias execuções simultâneas — cada uma com seu próprio conjunto de downloads de feed e chamadas de IA concorrentes. Isso já chegou a esgotar toda a memória do host: sem limite de container e sem swap configurado, o OOM killer do Linux reagiu derrubando processos do sistema inteiro, inclusive containers sem relação nenhuma com o Dr. Occam.

Como camada complementar, o `docker-compose.yml` define um teto de memória/CPU por serviço (`mem_limit`/`cpus` — não `deploy.resources.limits`, que só é respeitado em modo swarm, e este projeto roda com `docker compose up` normal): `api-occam` a 2GB/2 CPUs, `rsshub` a 512MB/1 CPU. Assim, mesmo que algo escape do controle dentro de um container, o pior caso passa a ser aquele container sendo reiniciado — não o host inteiro travando.

### Cascata de IAs (fallback)

`llm_processor.py` lê modelos numerados do ambiente (`LLM_MODEL_1`, `LLM_MODEL_2`, ...) e monta a cascata nativa de `fallbacks` do `litellm`: a chamada usa o modelo `1` como primário; se ele falhar (conexão recusada, timeout, rate limit etc.), o `litellm` tenta o `2`, depois o `3`, e assim por diante — de forma automática e silenciosa, sem que isso conte como falha de processamento do artigo (só falha de verdade se **todos** os modelos da cascata falharem). Cada modelo pode ter seu próprio `LLM_API_BASE_N`/`LLM_API_KEY_N`, o que permite misturar um LLM local (ex.: LM Studio numa outra máquina da rede, nem sempre ligada) com APIs comerciais na nuvem como fallback. `LLM_TIMEOUT_SECONDS` garante que uma máquina local desligada não trave a cascata por muito tempo antes de cair para o próximo modelo.

O ciclo completo (ingestão → processamento por IA) é disparado sob demanda via `POST /trigger-pipeline` (roda em background e responde imediatamente) ou automaticamente pelo agendador interno descrito acima.

### Busca semântica (`/search`)

Busca híbrida em linguagem natural (ex.: *"o que saiu sobre o Telegram nos últimos 30 dias"*), servida numa tela dedicada (`/search`, Tailwind via CDN, mesmo estilo do `/admin`) que consome `GET /api/search`. Combina duas etapas de IA com um índice vetorial local — nenhum serviço externo de busca/embeddings:

1. **Extração de filtro de data** (`app/search/intent_service.py`): uma chamada de chat (reaproveita a mesma cascata `LLM_CASCADE` do `llm_processor.py`, sem cascata própria) lê a busca e, se houver uma expressão temporal ("últimos 30 dias", "semana passada", "mês passado", "ontem" etc.), calcula `start_date`/`end_date` absolutos e devolve a busca sem a parte temporal (`cleaned_query`, usada na etapa seguinte). Falha (timeout, JSON malformado, cascata inteira fora do ar) degrada graciosamente para "sem filtro de data" — a busca semântica pura ainda funciona mesmo se essa etapa quebrar.
2. **Similaridade vetorial** (`app/search/repository.py` + `app/embeddings/`): `cleaned_query` é convertido em embedding e comparado por **distância de cosseno** com os embeddings dos artigos `PROCESSED` dentro da janela de data (se houver), usando a extensão [`sqlite-vec`](https://github.com/asg017/sqlite-vec) (tabela virtual `vec_articles`, `distance_metric=cosine`) — sem depender de um banco vetorial à parte (ChromaDB, pgvector etc.). A distância vira um percentual de relevância (0-100%) exibido como barra colorida (vermelho ≤40%, amarelo 41-70%, verde 71-100%).
3. **Piso de relevância** (`SEARCH_MIN_RELEVANCE_PERCENTAGE`, ver variáveis de ambiente abaixo): resultados abaixo do piso são descartados da resposta — sem isso, buscas vagas de uma palavra só (ex.: "atropelamento" sozinho) produzem similaridade de cosseno artificialmente parecida entre artigos sem relação nenhuma.
4. **Sugestões de refinamento** (`app/search/suggestion_service.py`): quando nenhum resultado passa do piso, uma chamada de IA separada sugere buscas mais específicas, com base nos títulos que até apareceram na busca (mesmo com relevância baixa) — pistas reais do acervo, não exemplos genéricos inventados. O prompt proíbe explicitamente perguntas/meta-comentário ("Você poderia especificar...") e há um filtro defensivo no parsing (`_looks_like_query`) que descarta qualquer sugestão nesse formato antes de devolver — necessário porque modelos locais menores às vezes ignoram a instrução do prompt, e o front-end usa a sugestão como texto literal da próxima busca (clicar num chip dispara `runSearch` de novo).

O vetor de cada artigo é gerado por `app/embeddings/service.py` (título + resumo da IA, truncado em `MAX_EMBEDDING_INPUT_CHARS`) via `litellm.aembedding` — mesmo padrão de cascata "provedor plugável" do resto do projeto (local via LM Studio, comercial via OpenAI etc.), guardado junto com metadados de proveniência na tabela `article_embedding` (modelo, dimensões, data). **Importante para modelos da família `nomic-embed-text`**: eles exigem prefixos de tarefa diferentes para texto indexado vs. texto de busca (`EMBEDDING_DOCUMENT_PREFIX`/`EMBEDDING_QUERY_PREFIX`) — sem isso o modelo não dá erro nenhum, só produz vetores mal discriminados (tudo parece ~60% relevante, relevante ou não).

#### Mantendo os embeddings atualizados

A geração de embeddings **ainda não é automática** — não está integrada ao `run_pipeline()` (é uma etapa manual, deliberadamente separada até o design da busca estar validado; ver Roadmap). Isso significa que todo artigo novo que o pipeline processa (`PENDING` → `PROCESSED`) fica **sem embedding e de fora da busca** até o script de backfill rodar. O script é idempotente — só processa artigos `PROCESSED` que ainda não têm embedding salvo (`get_article_ids_missing_embedding`), então rodá-lo de novo depois de cada varredura é seguro e rápido (não reprocessa o que já tem vetor).

Rodar o backfill **em primeiro plano** (bloqueia o terminal até terminar — bom para acompanhar o progresso em tempo real, ex. logo depois de zerar a base ou popular muitos artigos de uma vez):

```bash
docker exec -it dr_occam_api python -m embeddings.backfill
```

Rodar **em segundo plano** (recomendado para lotes grandes ou modelos locais lentos — não trava o terminal, e sobrevive a você fechar a sessão SSH):

```bash
docker exec -d dr_occam_api sh -c "python -m embeddings.backfill > /app/data/backfill.log 2>&1"
```

Acompanhar o progresso de uma execução em segundo plano (`Ctrl+C` só sai do `tail`, não interrompe o backfill):

```bash
docker exec -it dr_occam_api tail -f /app/data/backfill.log
```

Conferir quantos artigos ainda faltam (fora do container, direto no arquivo SQLite montado em `./app/data/occam.db`):

```bash
python3 -c "
import sqlite3
con = sqlite3.connect('app/data/occam.db')
cur = con.cursor()
cur.execute('''
    SELECT COUNT(*) FROM article a
    LEFT JOIN article_embedding e ON e.article_id = a.id
    WHERE a.status = 'PROCESSED' AND e.id IS NULL
''')
print('artigos PROCESSED sem embedding:', cur.fetchone()[0])
"
```

Se quiser reindexar tudo do zero (ex.: trocou de modelo de embedding, ou de dimensão) é preciso **derrubar a tabela vetorial e apagar os metadados** antes de rodar o backfill de novo — sem isso, artigos que já têm uma linha em `article_embedding` seriam pulados mesmo com o vetor antigo (de outro modelo/dimensão) parado na `vec_articles`:

```bash
docker exec -it dr_occam_api python -c "
from sqlmodel import Session, delete
from database import engine
from models import ArticleEmbedding

with engine.connect() as conn:
    conn.exec_driver_sql('DROP TABLE IF EXISTS vec_articles')
    conn.commit()

with Session(engine) as session:
    session.exec(delete(ArticleEmbedding))
    session.commit()

print('vec_articles derrubada e article_embedding zerada — rode o backfill de novo')
"
docker exec -it dr_occam_api python -m embeddings.backfill
```

Nenhum desses comandos exige rebuild de imagem — só `docker exec` num container que já está rodando.

## Modelo de dados

- **`source`**: fontes cadastradas (nome, URL, tipo `RSS`/`HTML_SCRAPE`, ativa, limite de artigos inéditos coletados por execução — `max_daily_articles`).
- **`article`**: cada notícia coletada — conteúdo original, status (`PENDING`/`PROCESSED`/`DEAD`/`DEDUPLICATED`), título e resumo gerados pela IA, `cluster_id`, contagem de tentativas e flag de truncamento. O enum de status também tem um valor `ARCHIVED`, mas é legado: era usado por um soft delete automático por cota diária que foi removido (arquivava artigos `PENDING` antes do LLM processá-los); nenhum código atual produz esse status, ele só existe pra não quebrar a leitura de linhas antigas já gravadas com ele.
- **`llm_processing_log`**: rastreabilidade de cada chamada de IA feita sobre um artigo (provider, versão do prompt, tokens consumidos, status).
- **`schedule_config`**: configuração persistida do agendador automático (linha única) — `enabled` e a lista `times` (horários `HH:MM`, formato `JSON`).
- **`article_embedding`**: proveniência do embedding de cada artigo (modelo, dimensões, data de geração) — o vetor em si não fica nessa tabela, mora na tabela virtual `vec_articles` da extensão `sqlite-vec` (o SQLModel/SQLAlchemy não mapeia esse tipo de tabela). Ver Busca semântica.

## Endpoints

| Método | Rota | Descrição |
|---|---|---|
| `POST` | `/trigger-pipeline` | Dispara ingestão + processamento por IA como background task. Responde `202` imediatamente, ou `409` se já houver uma execução em andamento (ver Proteção contra execução concorrente). |
| `POST` | `/pipeline/reprocess-dead` | Busca até `limit` artigos `DEAD` (padrão 20, máx. 200), volta o status para `PENDING` e zera `retry_count`, disparando o reprocessamento por IA como background task. Responde `202` com `{"articles_resurrected": N}`, ou `409` se já houver uma execução em andamento. |
| `GET` | `/feed.xml` | Feed RSS 2.0 dos artigos `PROCESSED`, mais recentes primeiro. |
| `GET` | `/` | Painel HTML mínimo para leitura rápida dos últimos artigos processados. |
| `GET` | `/admin` | Painel de administração (Jinja2 + Tailwind via CDN) para gerenciar as fontes. |
| `GET` | `/search` | Tela de busca semântica (Jinja2 + Tailwind via CDN), consome `/api/search`. |
| `GET` | `/api/search` | Busca híbrida: `q` (busca em linguagem natural, obrigatório) e `top_k` (máx. resultados, padrão 10, máx. 50). Retorna o filtro de data interpretado, os resultados com percentual de relevância e, se nenhum resultado passar do piso de relevância, sugestões de busca mais específicas. |
| `POST` | `/api/sources` | Cria uma fonte `RSS` nova (`{"name": ..., "url": ...}`). |
| `PATCH` | `/api/sources/{id}/toggle` | Alterna `active` da fonte. |
| `PUT` | `/api/sources/{id}/limit` | Atualiza `max_daily_articles` (`{"max_daily_articles": N}`). |
| `DELETE` | `/api/sources/{id}` | Remove a fonte. |
| `GET` | `/api/logs` | Últimas linhas de log em memória (`{"logs": [...]}`), usado pelo modal de logs do `/admin`. |
| `GET` | `/api/schedule` | Consulta a configuração do agendador automático (`{"enabled": bool, "times": [...]}`). |
| `PUT` | `/api/schedule` | Atualiza a configuração do agendador (liga/desliga e horários `HH:MM`, fuso `America/Sao_Paulo`) e reconfigura os jobs em tempo real, sem reiniciar o processo. |
| `DELETE` | `/api/articles?older_than_days=N` | Remove permanentemente artigos com `created_at` mais antigo que `N` dias, **qualquer status**, e os `llm_processing_log` associados a eles. Irreversível. Responde `{"articles_deleted": N, "logs_deleted": N}`. |
| `DELETE` | `/api/llm-logs?older_than_days=N` | Remove permanentemente registros de `llm_processing_log` cujo artigo associado tem `created_at` mais antigo que `N` dias (a idade é a do artigo — o log não tem timestamp próprio), mais qualquer log órfão (artigo já apagado). Irreversível. Responde `{"logs_deleted": N}`. |

## Variáveis de ambiente (`.env`)

| Variável | Descrição |
|---|---|
| `LLM_MODEL_1` | Modelo primário da cascata de IA, no formato aceito pelo `litellm` (ex.: `openai/<nome-do-modelo>` para um servidor OpenAI-compatible como o LM Studio). |
| `LLM_API_BASE_1` | Base URL do modelo primário (ex.: `http://<IP_DA_MAQUINA_NA_REDE>:1234/v1` do LM Studio). Opcional — só necessário para endpoints customizados. |
| `LLM_API_KEY_1` | Chave/token do modelo primário (LM Studio local geralmente aceita qualquer valor, ex.: `lm-studio`). |
| `LLM_MODEL_2` | Modelo de fallback, usado automaticamente se o primário falhar ou der timeout (ex.: `gemini/gemini-1.5-flash`). |
| `LLM_API_KEY_2` | Credencial do modelo de fallback. |
| `LLM_MODEL_N` / `LLM_API_BASE_N` / `LLM_API_KEY_N` | Padrão repetível para adicionar mais modelos à cascata (N = 3, 4, ...), sem alterar código. |
| `MAX_CONTENT_LENGTH` | Tamanho máximo (caracteres) do texto enviado à IA antes de truncar. |
| `NEGATIVE_KEYWORDS` | Palavras-chave (separadas por vírgula) que descartam uma notícia antes da extração. |
| `EMBEDDING_MODEL` | Modelo de embedding no formato `litellm` (default `text-embedding-3-small`). |
| `EMBEDDING_API_KEY` | Chave do provedor de embeddings. Se ausente, cai para `OPENAI_API_KEY`. |
| `EMBEDDING_API_BASE` | Base URL customizada (ex.: LM Studio local). Opcional. |
| `EMBEDDING_DIMENSIONS` | Dimensão do vetor — define o tamanho fixo da coluna da tabela `vec_articles` (mudar depois de criada exige recriar a tabela, ver seção "Mantendo os embeddings atualizados"). |
| `EMBEDDING_BATCH_SIZE` | Artigos por lote no backfill (default 50). |
| `EMBEDDING_MAX_CONCURRENCY` | Chamadas concorrentes ao provedor de embeddings (default 5). |
| `EMBEDDING_TIMEOUT_SECONDS` | Timeout por chamada de embedding, em segundos (default 60). |
| `EMBEDDING_DOCUMENT_PREFIX` / `EMBEDDING_QUERY_PREFIX` | Prefixo de tarefa prependido ao texto antes de embedar (default vazio) — obrigatório para modelos `nomic-embed-text` (`"search_document: "` / `"search_query: "`); deixe vazio para provedores como OpenAI. |
| `INTENT_LLM_TIMEOUT_SECONDS` | Timeout das chamadas de chat auxiliares da busca (extração de data, sugestões de refinamento) — default 10s, aumente se seu modelo local for lento. |
| `SEARCH_MIN_RELEVANCE_PERCENTAGE` | Piso de relevância (0-100) abaixo do qual um resultado é descartado da busca (default 65). |

## Como rodar

```bash
# 1. Preencha as chaves reais em .env (LLM_MODEL_1/LLM_API_BASE_1, LLM_MODEL_2/LLM_API_KEY_2, etc.)
docker compose up -d --build

# 2. Cadastre fontes pelo painel de admin (ou use o script auxiliar add_source.py como atalho):
# http://localhost:8383/admin

# 3. Dispare o pipeline manualmente (ou clique em "Executar varredura" no /admin)
curl -X POST http://localhost:8383/trigger-pipeline

# 4. Consuma o resultado
# http://localhost:8383/         (painel de leitura)
# http://localhost:8383/feed.xml (RSS)
```

## Estado atual vs. visão do produto

Já implementado: ingestão RSS, filtro anti-ruído, limite de artigos coletados por execução por fonte, extração de conteúdo in-process via trafilatura (sequencial, sem microsserviço externo), cascata de IAs com fallback automático (local + nuvem), resumo neutro via IA com log de rastreabilidade, retry/DEAD com fila standby de repescagem de timeout, reprocessamento manual de artigos `DEAD` (`/pipeline/reprocess-dead`, com botão dedicado no `/admin`), feed RSS 2.0, painel de leitura HTML, painel de administração de fontes (`/admin`, CRUD completo, responsivo em mobile), agendador automático interno via `APScheduler` configurável pelo `/admin` (manual, diário ou múltiplas vezes ao dia, persistido em banco e reconfigurável sem restart), gatilho manual assíncrono, trava contra execução concorrente do pipeline (`app/pipeline_state.py`) e teto de memória/CPU por container no `docker-compose.yml`, limpeza manual de dados antigos por idade (`DELETE /api/articles`, `DELETE /api/llm-logs`) para conter o crescimento do banco, e busca semântica híbrida (`/search`, filtro de data em linguagem natural + similaridade de cosseno via `sqlite-vec` + piso de relevância + sugestões de refinamento, com backfill de embeddings manual — ver seção dedicada).

Ainda não implementado (fazem parte da especificação de negócio original, em `negocio.md`):

- **Deduplicação/agrupamento** de notícias do mesmo evento (`cluster_id` existe no schema, mas nunca é preenchido ainda).
- **Monitoramento explícito de rate limit (HTTP 429)** como gatilho de fallback — hoje a cascata do `litellm` já pula de modelo em qualquer exceção (conexão, timeout, rate limit), mas não há tratamento diferenciado por tipo de erro.
- **Tradução explícita** de fontes multi-idioma como etapa dedicada do pipeline.
- **Fontes `HTML_SCRAPE`** — o schema já suporta o tipo, mas nem o `/admin` (o formulário só cria `RSS`) nem a ingestão (que só sabe extrair links de `RSS`) têm esse caminho implementado.
- **Etiqueta de rastreio no painel de leitura** ("Processado por: X | Prompt: vY") — o dado já é gravado em `llm_processing_log`, mas ainda não aparece na UI pública.
- **Backfill de embeddings automático** — hoje é um script manual (`python -m embeddings.backfill`, ver seção "Mantendo os embeddings atualizados"); a ideia é integrá-lo ao final de `run_pipeline()` para todo artigo recém-processado já sair com embedding, sem esse passo manual.
- **Link "buscar" no feed RSS/leitores externos** — a busca semântica hoje só é acessível pelo painel (`/search`), não tem exposição nenhuma para quem consome só o `/feed.xml`.

### Ideia futura: auto-detecção de rota RSSHub ao cadastrar fonte

Hoje, pra cadastrar um site sem RSS nativo (ex.: `bbc.com/technology`), o usuário precisa descobrir manualmente a rota certa do RSSHub (`docs.rsshub.app`) e colar a URL já traduzida (`http://rsshub:1200/bbc/technology`) no `/admin`. A ideia é automatizar isso: o usuário cola o link original do site e o sistema descobre/sugere a rota RSSHub sozinho.

**Viabilidade — mecanismo existe e é acessível:**

- O RSSHub tem um sistema nativo pra isso, o **Radar**: cada rota pode declarar uma regra mapeando um padrão de URL do site original (`source`) para o template da própria rota (`target`). É o mesmo mecanismo que alimenta a extensão de navegador "RSSHub Radar".
- Qualquer instância do RSSHub expõe essas regras via `GET /api/radar/rules` — no nosso caso, `http://rsshub:1200/api/radar/rules`. Retorna um JSON indexado por domínio, com os padrões `source`/`target` de cada rota conhecida (ex.: `bbc.com` → `source: "/:channel?"`, `target: "/bbc/:channel"`).
- Fluxo proposto: usuário cola `https://www.bbc.com/technology` → backend extrai domínio + path → busca o domínio no JSON de regras (cacheado localmente, refresh periódico) → casa o path contra o padrão `source` e extrai os parâmetros → substitui no `target` → gera a URL RSSHub final → **valida antes de salvar** (faz um GET real na URL gerada e confere se voltou um XML de feed com itens, não só HTTP 200 — a própria página de erro do RSSHub retorna corpo HTML) → salva como `Source(source_type=RSS, url=...)`.

**Limitações a considerar antes de implementar:**

- Cobertura não é universal — só domínios com regra de Radar cadastrada pelos mantenedores do RSSHub aparecem no JSON; um site menor pode ter rota funcional sem regra de Radar (ou nem ter rota nenhuma).
- O casamento `source`/`target` usa sintaxe estilo `path-to-regexp` (parâmetros opcionais, wildcards) — não é regex trivial, precisa de um matcher dedicado (portar/reimplementar em Python, já que o `api-occam` não roda Node).
- Alguns parâmetros de rota não existem na URL visível da página (ex.: a rota `/bbc/topics/:topic` pede um ID que só aparece dentro do HTML/URL de uma página de tópico específica do BBC) — nesses casos a auto-detecção falha e precisa cair pra um fluxo manual de qualquer forma.
- Por isso, qualquer implementação futura precisa de um fallback gracioso ("não encontrei rota automática pra esse domínio, cadastre manualmente") em vez de assumir que todo link terá sucesso.

### Ideia futura: ativar `--reload` do uvicorn (evitar rebuild a cada ajuste)

Hoje, qualquer alteração em código (`.py`) só é refletida no container depois de um ciclo completo `docker compose down` + `docker compose up -d --build` — lento e repetitivo durante desenvolvimento iterativo.

**Causa raiz:** o `docker-compose.yml` já monta `./app:/app` por volume (o próprio comentário no arquivo diz "para hot-reload"), mas o `CMD` do `Dockerfile` nunca foi ajustado pra aproveitar isso — o `uvicorn` sobe sem a flag `--reload`, então o processo dentro do container não percebe mudança nenhuma sozinho.

**Correção proposta:** trocar o `CMD` do `Dockerfile` (`app/Dockerfile:21`) para incluir `--reload`, por exemplo:

```
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
```

Como o código já é bind-mount (não uma cópia estática da imagem), o `uvicorn` passa a detectar sozinho quando um arquivo `.py` muda e reinicia o processo automaticamente (~1s) — sem precisar de nenhum comando manual. Templates Jinja2 (`app/templates/*.html`) já são lidos do disco a cada requisição por padrão, então esses já deveriam refletir mudança só com F5, independente do `--reload`.

**Ressalvas:**

- Precisa de **um rebuild único** pra essa mudança de `CMD` entrar em vigor (depois disso, edits de código não pedem mais rebuild).
- Mudanças em `requirements.txt` (dependência nova) continuam exigindo `docker compose up -d --build` — pacotes instalados ficam "assados" na imagem, o `--reload` não cobre isso.
- É um watcher de arquivos rodando o tempo todo — custo de CPU/RAM desprezível pro uso pessoal/dev, mas foge um pouco do princípio de "leveza" do `negocio.md` pensado pra produção numa Raspberry Pi. Não é recomendado deixar `--reload` ativo num ambiente de produção "real" — só faz sentido enquanto o projeto está em desenvolvimento ativo.
- Não precisa mexer em nada no CasaOS pra isso — o botão de restart/recreate dele já roda `docker compose` por baixo dos panos; resolvendo o `--reload`, o CasaOS nem entra na equação pra esse tipo de ajuste.

## Observação importante

A extração de conteúdo trocou de um microsserviço externo (Browserless, que abria uma instância de Chromium por notícia e chegava a consumir 100% de CPU/RAM com poucas raspagens simultâneas) para `trafilatura`, uma biblioteca leve que baixa e extrai o texto principal do artigo dentro do próprio processo do `api-occam`. Como consequência, o `docker-compose.yml` voltou a ter um único container. Para manter o consumo de recursos baixo, os artigos de uma mesma fonte são raspados **um de cada vez** (`app/ingestion.py`), com uma pausa de `SCRAPE_DELAY_SECONDS` (2s, fixo no código) entre cada extração — isso é deliberadamente conservador; fontes diferentes ainda podem ser processadas em paralelo entre si (`MAX_CONCURRENT_FEEDS`).

`trafilatura` depende só de requisições HTTP simples (sem JavaScript) para baixar a página — sites fortemente dependentes de renderização client-side (SPAs) podem não ter o conteúdo capturado corretamente. Se isso virar um problema recorrente, um serviço de renderização (tipo o Browserless removido) volta a ser necessário para essas fontes específicas.

Bloqueios de origem (ex.: 401 anti-bot/paywall da Reuters) e instabilidade do RSSHub local (ex.: 503 sob rate-limit em rotas como `/bbc/travel`) são tratados em duas camadas, ambas em `app/ingestion.py`: retry com backoff exponencial para status transitórios (`429`/`502`/`503`/`504`) nas requisições HTTP, e uma cadeia de extração (`_EXTRACTORS`) que cai para o texto de `<content:encoded>`/`<description>` do próprio feed quando o `trafilatura` não consegue baixar ou extrair a página. Esse fallback só é aceito acima de `MIN_FALLBACK_CONTENT_LENGTH` caracteres (hoje 80, calibrado para os resumos curtos que a Reuters entrega no feed).

O diretório `./scraper-akita` (Frank Investigator) não é referenciado pelo `docker-compose.yml` — ele fica no repo sem uso pelo Dr. Occam por enquanto.

`DELETE /api/sources/{id}` remove só a linha da fonte — a tabela `source` não tem `ON DELETE CASCADE` (nem SQLite aplica `FOREIGN KEY` por padrão), então artigos já coletados dessa fonte continuam no banco com um `source_id` órfão em vez de serem apagados junto. Isso é intencional (excluir uma fonte não deveria apagar o histórico de notícias já processadas), mas vale saber que o artigo antigo não desaparece do feed/painel só porque a fonte foi removida.
