# Installazione — Web Search Hub MCP

## Prerequisiti
- **Docker Engine + docker compose v2**; utente nel gruppo `docker`.
- **Tailscale** attivo sull'host. Annota l'IP: `tailscale ip -4`.
- **Egress internet** (DDGS + SearXNG).
- Per test/`how_to_use.py`: un venv Python con il pacchetto `mcp` (su questo host: `/home/llmadmin/venv`).

## Layout sorgenti (`/opt/mcp-search-hub`)
```
docker-compose.yml
mcp-web-search/
├── mcp-hub/
│   ├── Dockerfile
│   └── src/  (.env, hub.py, backends.py, config.py, rate_limiter.py)
└── searxng/settings.yml
```

## Adattamenti al NUOVO host (3 punti — gotcha noti dalla migrazione 496→219)
Nel `docker-compose.yml`:
1. **Volume SearXNG** → path **RELATIVO** al compose dir:
   `./mcp-web-search/searxng/settings.yml:/etc/searxng/settings.yml:ro`
   (NON un path assoluto di un altro host: Docker creerebbe una dir vuota → SearXNG senza config).
2. **`ports` di `mcp_hub`** → bind sull'**IP Tailscale DI QUESTO host**, es. `"100.94.187.21:8765:8765"`.
   Bind su un IP non locale ⇒ `cannot assign requested address`.
3. **`mcp_hub`** → aggiungi il **build locale** (l'immagine non è in alcun registry):
   ```yaml
   build:
     context: ./mcp-web-search/mcp-hub
   image: mcp-web-search-hub:latest
   ```

## Pre-flight
- Porta 8765 libera: `ss -tlnp | grep :8765` (attenzione a run manuali/`systemd` di `hub.py`).
- Nessun SearXNG "rogue" su `0.0.0.0:8080` da run manuali: `docker ps | grep searxng`.

## Avvio
```
docker compose -f /opt/mcp-search-hub/docker-compose.yml up -d --build
docker compose -f /opt/mcp-search-hub/docker-compose.yml ps
docker logs --tail 30 mcp_web_search_hub_secure
```

## Verifica
```
curl -s -o /dev/null -w '%{http_code}\n' -N --max-time 4 http://<IP_TAILSCALE>:8765/sse   # atteso 200
ss -tlnp | grep :8765                                                                     # listener sull'IP Tailscale
./how_to_use.sh                                                                            # schema + self-test live
```

## Registrazione client (Claude Code, user-scope)
```
claude mcp add --scope user --transport sse web-search-hub http://<IP_TAILSCALE>:8765/sse
claude mcp list
```
NB: dopo il cambio endpoint, **riavvia il client** (la connessione MCP è caricata all'avvio).

## Config runtime (`mcp-web-search/mcp-hub/src/.env`)
Parametri principali: `SEARXNG_URL` (Docker DNS del sibling, `http://searxng_engine_secure:8080`),
`HOST`/`PORT`, e i rate limit (`DDGS_RATE_LIMIT`, `DDGS_BURST`, `DDGS_COOL_DOWN`, idem `SEARXNG_*`).

## Sicurezza
- SearXNG isolato su `hermes_search_net`, **nessuna porta pubblicata**.
- `mcp_hub` esposto **solo** sull'IP Tailscale (non `0.0.0.0`).
- `searxng/settings.yml` contiene un `secret_key`: **rigeneralo** per istanze/ambienti distinti.

## Rollback / migrazione
- Backup compose automatici: `docker-compose.yml.bak.*`.
- Per spostare host: ripeti i 3 adattamenti con il nuovo IP Tailscale; il vecchio stack si può
  tenere **stoppato** (`docker compose stop`) come rollback (riavvio con `... start`).
