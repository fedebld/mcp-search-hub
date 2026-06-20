"""Hub configuration via pydantic-settings (env + .env file)."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # SearXNG — inside Docker: reach sibling container via Docker DNS
    searxng_url: str = "http://searxng-engine:8080"
    searxng_timeout: int = 10
    searxng_host_header: str = "localhost"

    # DDGS
    ddgs_timeout: int = 10
    ddgs_proxy: str | None = None

    # Rate limiting
    ddgs_rate_limit: int = 6
    ddgs_burst: int = 3
    ddgs_cool_down: int = 300
    searxng_rate_limit: int = 12
    searxng_burst: int = 6
    searxng_cool_down: int = 120

    # Server
    host: str = "0.0.0.0"
    port: int = 8765

    # Jina API (optional)
    jina_api_key: str = ""

    # Cache risultati ricerche (in-memory TTL+LRU; abbatte chiamate backend e pressione rate limiter)
    cache_enabled: bool = True
    cache_max_size: int = 512
    cache_ttl_web: int = 900       # 15 min
    cache_ttl_news: int = 300      # 5 min (news time-sensitive)
    cache_ttl_image: int = 3600    # 1 h
    cache_ttl_extract: int = 3600  # 1 h
    # Persistenza cache su SQLite (montato su volume): sopravvive ai restart del container
    cache_persistent: bool = True
    cache_db_path: str = "/app/data/cache.db"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
