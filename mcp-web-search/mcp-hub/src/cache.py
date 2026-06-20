"""TTL + LRU cache dei risultati di ricerca, con persistenza opzionale su SQLite.

Riduce le chiamate ai backend (DDGS/SearXNG) e la pressione sul rate limiter:
una query gia' vista entro il TTL ritorna dalla cache senza consumare budget.
Async-safe (asyncio.Lock). Solo stdlib.

Persistenza: se db_path e' valorizzato, ogni set e' scritto in write-through su
SQLite (montato su volume) e le voci valide sono ricaricate allo startup -> la
cache sopravvive ai restart del container. Le scadenze sono in tempo ASSOLUTO
(time.time()) proprio per restare valide fra un restart e l'altro.
"""
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time

logger = logging.getLogger("cache")


class TTLCache:
    """Cache TTL+LRU async-safe, con backing SQLite opzionale."""

    def __init__(self, max_size: int = 512, default_ttl: int = 900, db_path: str | None = None):
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._store: dict[str, tuple[float, object]] = {}  # key -> (expires_at_epoch, value)
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0
        self._db_path = db_path
        self._db: sqlite3.Connection | None = None
        if db_path:
            self._init_db()

    @property
    def persistent(self) -> bool:
        return self._db is not None

    # ---------- SQLite (L2 durevole) ----------
    def _init_db(self):
        try:
            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
            self._db = sqlite3.connect(self._db_path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS cache "
                "(key TEXT PRIMARY KEY, expires_at REAL NOT NULL, value TEXT NOT NULL)"
            )
            self._db.commit()
            self._load()
        except Exception as exc:
            logger.error("[cache] init SQLite fallita (%s): fallback in-memory", exc)
            self._db = None

    def _load(self):
        now = time.time()
        self._db.execute("DELETE FROM cache WHERE expires_at < ?", (now,))
        self._db.commit()
        rows = self._db.execute(
            "SELECT key, expires_at, value FROM cache ORDER BY expires_at DESC LIMIT ?",
            (self._max_size,),
        ).fetchall()
        for key, exp, value in rows:
            try:
                self._store[key] = (exp, json.loads(value))
            except json.JSONDecodeError:
                continue
        logger.info("[cache] caricate %d voci da SQLite (%s)", len(self._store), self._db_path)

    def _db_set(self, key, expires_at, value):
        self._db.execute(
            "INSERT OR REPLACE INTO cache (key, expires_at, value) VALUES (?, ?, ?)",
            (key, expires_at, json.dumps(value, ensure_ascii=False, default=str)),
        )
        self._db.commit()

    def _db_delete(self, keys):
        if not keys:
            return
        self._db.executemany("DELETE FROM cache WHERE key = ?", [(k,) for k in keys])
        self._db.commit()

    # ---------- API ----------
    @staticmethod
    def make_key(tool: str, args: dict) -> str:
        blob = json.dumps({"tool": tool, "args": args}, sort_keys=True,
                          ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    async def get(self, key: str):
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            expires_at, value = entry
            if expires_at < time.time():
                self._store.pop(key, None)  # scaduto
                if self._db is not None:
                    await asyncio.to_thread(self._db_delete, [key])
                self._misses += 1
                return None
            # LRU touch: riposiziona in coda (i piu' vecchi restano in testa)
            self._store.pop(key, None)
            self._store[key] = (expires_at, value)
            self._hits += 1
            return value

    async def set(self, key: str, value, ttl: int | None = None):
        ttl = self._default_ttl if ttl is None else ttl
        if ttl <= 0:
            return
        expires_at = time.time() + ttl
        async with self._lock:
            self._store[key] = (expires_at, value)
            evicted = []
            if len(self._store) > self._max_size:
                now = time.time()
                for k in [k for k, (exp, _) in self._store.items() if exp < now]:
                    self._store.pop(k, None)  # purga scaduti
                    evicted.append(k)
                while len(self._store) > self._max_size:
                    oldest = next(iter(self._store))  # LRU: testa
                    self._store.pop(oldest, None)
                    evicted.append(oldest)
            if self._db is not None:
                await asyncio.to_thread(self._db_set, key, expires_at, value)
                if evicted:
                    await asyncio.to_thread(self._db_delete, evicted)

    @property
    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "hits": self._hits, "misses": self._misses,
            "size": len(self._store), "max_size": self._max_size,
            "hit_ratio": round(self._hits / total, 3) if total else 0.0,
        }
