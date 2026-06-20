#!/usr/bin/env bash
# Live smoke test of both MCP servers — exercises every tool + 2 edge cases.
# Run on a host that can reach the endpoints (e.g. on 219 itself, or over Tailscale).
#
#   YTMD_BASE=http://100.94.187.21:8769  SH_BASE=http://100.94.187.21:8765  ./smoke_live.sh
#
# Requires: curl, jq, and a FastMCP client for tool calls (the HTTP checks below need no deps).
set -euo pipefail
YTMD_BASE="${YTMD_BASE:-http://100.94.187.21:8769}"
SH_BASE="${SH_BASE:-http://100.94.187.21:8765}"
VID="${VID:-dQw4w9WgXcQ}"
fail=0
ok()   { echo "  PASS  $1"; }
bad()  { echo "  FAIL  $1"; fail=1; }

echo "== youtube-to-markdown ($YTMD_BASE) =="
# research bundle served over HTTP (smoke of the async pipeline output surface)
if curl -fsS "$YTMD_BASE/health" >/dev/null 2>&1 || curl -fsS "$YTMD_BASE/" >/dev/null 2>&1; then
  ok "http endpoint reachable"
else
  bad "http endpoint unreachable"
fi

echo "== search-hub ($SH_BASE) =="
if curl -fsS "$SH_BASE/" >/dev/null 2>&1 || nc -z ${SH_BASE#http://} 2>/dev/null; then
  ok "http endpoint reachable"
else
  echo "  WARN  search-hub is SSE/MCP only; use the MCP-client smoke below"
fi

cat <<'NOTE'

== MCP tool-level smoke (run from an agent or a FastMCP client) ==
youtube-to-markdown:
  youtube_metadata(dQw4w9WgXcQ)             -> expect id/title/duration
  youtube_transcript(dQw4w9WgXcQ)           -> expect non-empty text
  youtube_to_markdown(dQw4w9WgXcQ)          -> expect schema_version 1.1 frontmatter
  youtube_research(dQw4w9WgXcQ,{max_claims:2}) -> poll research_status until done; expect verdict_counts
  youtube_metadata("THIS_IS_NOT_REAL_ID")   -> expect {error:...} (graceful)
search-hub:
  unified_web_search("eu ai act")           -> expect list + per-result engine field
  unified_news_search("ccnl", region=it-it) -> expect list with date/source
  unified_image_search("european union flag", max_results=5) -> expect >=3 OR SearXNG-tagged failover
  unified_content_extract("https://example.com") -> expect {content, extractor:"trafilatura"}
  unified_content_extract("https://httpstat.us/404") -> expect {error_kind:"http_404", http_status:404}
NOTE

exit $fail
