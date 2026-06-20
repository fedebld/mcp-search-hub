# Web Search Hub — MCP Skill (framework per agent)

MCP server self-hosted di *information retrieval*: ricerca **web / news / immagini** ed
**estrazione pagine** in markdown. Failover **DDGS → SearXNG** + circuit breaker, con
**cache persistente** dei risultati. Nessuna API a pagamento.

## Connessione
- **Transport:** SSE — **Endpoint:** `http://100.94.187.21:8765/sse` (host `vLL-vault` / VMID 219).
- Raggiungibile **solo via Tailscale**: se il nodo client non è connesso a Tailscale, i tool falliscono.
- Registrazione (Claude Code, user-scope):
  ```
  claude mcp add --scope user --transport sse web-search-hub http://100.94.187.21:8765/sse
  ```
- **Gotcha:** il client carica la connessione MCP **all'avvio**; dopo un cambio di endpoint, **riavvia il client**.

## Tool esposti (firme reali)

| Tool | Parametri | Ritorna | Backend |
|------|-----------|---------|---------|
| `unified_web_search` | `query:str`, `max_results:int=10`, `timelimit:str=None`, `region:str="us-en"` | `list[dict]{title,href,body}` | DDGS → SearXNG |
| `unified_news_search` | `query:str`, `max_results:int=10`, `timelimit:str=None`, `region:str="us-en"` | `list[dict]{title,url,source,date}` | DDGS news → fallback web |
| `unified_image_search` | `query:str`, `max_results:int=10`, `region:str="us-en"` | `list[dict]` immagini | **solo DDGS** (no failover) |
| `unified_content_extract` | `url:str`, `fmt:str="text_markdown"` | `dict{url,content,extractor}` | **trafilatura** reader-view (boilerplate-removal) → fallback DDGS/SearXNG |
| `cache_stats` | *(nessuno)* | `dict{hits,misses,size,max_size,hit_ratio,enabled,persistent}` | **locale** (no backend, read-only) |

Dettagli e cavetti:
- `timelimit`: `d` | `w` | `m` | `y` (giorno/settimana/mese/anno) oppure `None`.
- `region`: es. `us-en`, `it-it`. `max_results` consigliato **1–20**.
- `fmt` di extract: `text_markdown` | `text_plain`. Contenuto **troncato a 50.000 char**; `body` web via SearXNG troncato a ~500 char.
- **`unified_web_search` in caso di guasto totale ritorna un `dict` `{"error":..., "query":...}`, NON una lista** → l'agent deve gestire entrambi i tipi.
- `unified_image_search` **non ha failover**: in errore ritorna `{"error":...}`.
- `unified_news_search` in fallback chiama internamente `unified_web_search("news: "+query)` → **consuma doppio budget**.

### Esempi di payload
```json
unified_web_search   {"query":"AI Act 2026","max_results":5,"timelimit":"m","region":"it-it"}
unified_news_search  {"query":"intelligenza artificiale","max_results":5,"timelimit":"w","region":"it-it"}
unified_image_search {"query":"golden retriever","max_results":8}
unified_content_extract {"url":"https://example.com/articolo","fmt":"text_markdown"}
cache_stats          {}
```

## ⚠️ Rate limiter + circuit breaker — IMPLICAZIONI TRASVERSALI
Il hub applica **due rate limiter GLOBALI e CONDIVISI** (token-bucket + circuit breaker),
**non** per-client né per-tool:

| Limiter | Rate | Burst | Cooldown | Soglia guasti |
|---------|------|-------|----------|---------------|
| **DDGS** | 6/min | 3 | **300s (5 min)** | 3 |
| **SearXNG** | 12/min | 6 | 120s | 5 |

**Punto chiave:** *tutti e 4 i tool di ricerca colpiscono PRIMA DDGS*. Quindi i **6 req/min DDGS** sono
il collo di bottiglia reale **condiviso** da web/news/image/extract **e da tutti gli agent** sullo
stesso endpoint. (Un **cache HIT NON consuma budget** → vedi sezione Cache: è la difesa principale.)

In caso di uso eccessivo:
1. **Rate superato** → `RateLimitError`: `web_search`/`extract` ripiegano su SearXNG; `image_search`
   ritorna `{"error"}`; `news` ripiega su `web_search` (consumando ancora limiter).
2. **Dopo 3 guasti consecutivi (DDGS) il circuito si APRE** → `CircuitOpenError` per **tutto il
   cooldown (300s)** → in quella finestra **tutti i tool DDGS sono giù per OGNI agent**.
3. Endpoint **multi-agent condiviso**: un singolo agent che martella può aprire il circuito e
   **accecare web/news/extract di tutti gli altri per 5 minuti**.
4. **I retry contano come guasti**: su errore rate/circuito → **backoff esponenziale**, mai retry-storm.
5. `news` in fallback = 2 chiamate; preferisci **1 chiamata con `max_results` alto (≤20)**. **Cache** i risultati.

**Regole d'oro per agent:** batch (poche chiamate, `max_results` alto) · gestisci sia `list` sia
`dict{"error"}` · backoff su errore · **non** eseguire `how_to_use.py`/self-test in loop.

## Cache dei risultati
Il hub mette in cache i **risultati validi** (TTL+LRU, **persistente su SQLite/volume**).
- **Persistenza:** L1 in-memory (HIT in ~ms) + L2 **SQLite write-through** su volume Docker
  `mcp_cache_data:/app/data` (`cache.db`, WAL). Allo startup la cache viene **ricaricata da SQLite**
  → **sopravvive ai restart** del container. Scadenze in tempo assoluto. Init DB fallita → fallback in-memory.
- **TTL:** web 15min · news 5min · image 1h · extract 1h — configurabili in `.env`
  (`CACHE_TTL_*`, `CACHE_MAX_SIZE`, `CACHE_ENABLED`, `CACHE_PERSISTENT`, `CACHE_DB_PATH`).
- **Chiave** = tool + *tutti* gli argomenti (`query`, `max_results`, `timelimit`, `region`, `fmt`, `url`):
  cambiare anche un solo argomento = miss.
- **Un cache HIT NON consuma il rate budget** (né DDGS né SearXNG) e ritorna in ~ms → ripetere la
  stessa query è gratis e istantaneo. È la leva principale contro l'apertura del circuito.
- Si cachano **solo** risultati validi (mai errori/vuoti) → dopo un errore un retry riprova davvero.
- **Condivisa multi-agent:** se un agent ha già cercato X, gli altri ottengono l'hit.
- **Osservabilità:** il tool **`cache_stats`** (read-only) espone `hits/misses/size/max_size/hit_ratio/enabled/persistent`.
- **Trade-off freshness:** i risultati possono essere vecchi fino al TTL. Per dati freschissimi usa
  `unified_news_search` (TTL 5min) o vincola con `timelimit`. Kill-switch: `CACHE_ENABLED=false`.

## Quando usare cosa
- Fatti/pagine attuali, link → `unified_web_search`.
- Attualità/cronaca recente → `unified_news_search` (+ `timelimit`).
- Immagini → `unified_image_search`.
- Leggere il contenuto di un URL noto → `unified_content_extract`.
- Diagnostica/osservabilità cache (ops) → `cache_stats`.
