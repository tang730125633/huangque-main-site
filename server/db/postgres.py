"""Lazy PostgreSQL pool. Importing this module never opens a connection."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager

DATABASE_URL_ENV = "HQ_DATABASE_URL"
_pool = None
_pool_lock = threading.Lock()


def configured() -> bool:
    return bool((os.environ.get(DATABASE_URL_ENV) or "").strip())


def sqlalchemy_url(url: str | None = None) -> str:
    """Return the explicit SQLAlchemy psycopg 3 dialect URL."""
    value = (url if url is not None else os.environ.get(DATABASE_URL_ENV) or "").strip()
    if not value:
        raise RuntimeError(f"{DATABASE_URL_ENV} is not configured")
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value[len("postgresql://"):]
    return value


def _positive_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be positive")
    return value


def _settings() -> dict:
    minimum = _positive_int("HQ_DB_POOL_MIN", 1)
    maximum = _positive_int("HQ_DB_POOL_MAX", 10)
    if minimum > maximum:
        raise RuntimeError("HQ_DB_POOL_MIN cannot exceed HQ_DB_POOL_MAX")
    return {
        "min_size": minimum,
        "max_size": maximum,
        "timeout": _positive_int("HQ_DB_POOL_TIMEOUT", 10),
    }


def _get_pool():
    global _pool
    if _pool is not None:
        return _pool
    url = (os.environ.get(DATABASE_URL_ENV) or "").strip()
    if not url:
        raise RuntimeError(f"{DATABASE_URL_ENV} is not configured")
    with _pool_lock:
        if _pool is None:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            settings = _settings()
            candidate = ConnectionPool(
                conninfo=url,
                min_size=settings["min_size"],
                max_size=settings["max_size"],
                timeout=settings["timeout"],
                kwargs={"autocommit": False, "row_factory": dict_row},
                open=False,
            )
            candidate.open(wait=True, timeout=settings["timeout"])
            _pool = candidate
    return _pool


@contextmanager
def connection():
    with _get_pool().connection() as conn:
        yield conn


@contextmanager
def transaction():
    with connection() as conn:
        with conn.transaction():
            yield conn


def healthcheck() -> bool:
    with connection() as conn:
        return conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1


def close_pool() -> None:
    global _pool
    with _pool_lock:
        current, _pool = _pool, None
    if current is not None:
        current.close()
