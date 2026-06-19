"""PostgreSQL connection management for the Telemetry Analytics Platform.

This is the **Storage** boundary referenced in ``docs/project_context.md`` §7.
It provides an environment-configured, pooled, async-friendly connection
manager built around ``asyncpg``.

Design notes:

* **Postgres-ready, not Postgres-required.** ``asyncpg`` is imported lazily so
  the module (and the FastAPI app) import cleanly even when the driver or a live
  database is unavailable. The ingestion edge can boot and report ``degraded``
  health rather than failing outright.
* **Connection pooling structure** is owned here; callers acquire connections
  via the :meth:`DatabaseManager.acquire` async context manager.
* All configuration comes from environment variables (see :class:`DatabaseConfig`).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)

# Imported lazily in ``connect()`` so the module loads without the driver.
try:  # pragma: no cover - import guard
    import asyncpg  # type: ignore
except Exception:  # pragma: no cover
    asyncpg = None  # type: ignore


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid int for %s=%r; using default %s", name, raw, default)
        return default


@dataclass
class DatabaseConfig:
    """Connection + pool configuration, sourced from environment variables.

    Either provide a full ``DATABASE_URL`` (DSN) or the discrete ``POSTGRES_*``
    parts. The DSN takes precedence when present.
    """

    dsn: Optional[str] = None
    host: str = "localhost"
    port: int = 5432
    database: str = "telemetry"
    user: str = "postgres"
    password: str = ""

    # Pool sizing / behaviour.
    pool_min_size: int = 1
    pool_max_size: int = 10
    command_timeout: float = 30.0

    # Extra connect kwargs forwarded to asyncpg (e.g. ssl).
    connect_kwargs: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "DatabaseConfig":
        return cls(
            dsn=os.getenv("DATABASE_URL") or None,
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=_env_int("POSTGRES_PORT", 5432),
            database=os.getenv("POSTGRES_DB", "telemetry"),
            user=os.getenv("POSTGRES_USER", "postgres"),
            password=os.getenv("POSTGRES_PASSWORD", ""),
            pool_min_size=_env_int("DB_POOL_MIN_SIZE", 1),
            pool_max_size=_env_int("DB_POOL_MAX_SIZE", 10),
            command_timeout=float(os.getenv("DB_COMMAND_TIMEOUT", "30")),
        )

    def resolved_dsn(self) -> str:
        if self.dsn:
            return self.dsn
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    def safe_repr(self) -> str:
        """DSN with the password redacted, for logging."""
        if self.dsn:
            return "DATABASE_URL=<provided>"
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.database}"


class DatabaseManager:
    """Manages an ``asyncpg`` connection pool lifecycle.

    Typical usage (wired into the FastAPI lifespan in ``api/app.py``)::

        db = DatabaseManager(DatabaseConfig.from_env())
        await db.connect()
        async with db.acquire() as conn:
            await conn.execute(...)
        await db.disconnect()
    """

    def __init__(self, config: Optional[DatabaseConfig] = None) -> None:
        self.config = config or DatabaseConfig.from_env()
        self._pool: Optional["asyncpg.Pool"] = None  # type: ignore[name-defined]

    @property
    def is_connected(self) -> bool:
        return self._pool is not None

    @property
    def pool(self) -> "asyncpg.Pool":  # type: ignore[name-defined]
        if self._pool is None:
            raise RuntimeError(
                "Database pool is not initialized. Call connect() first."
            )
        return self._pool

    async def connect(self) -> None:
        """Create the connection pool. Idempotent."""
        if self._pool is not None:
            return
        if asyncpg is None:
            raise RuntimeError(
                "asyncpg is not installed. Install it (see requirements.txt) "
                "to enable database connectivity."
            )
        logger.info("Connecting to database (%s)", self.config.safe_repr())
        self._pool = await asyncpg.create_pool(
            dsn=self.config.resolved_dsn(),
            min_size=self.config.pool_min_size,
            max_size=self.config.pool_max_size,
            command_timeout=self.config.command_timeout,
            **self.config.connect_kwargs,
        )
        logger.info(
            "Database pool ready (min=%s max=%s)",
            self.config.pool_min_size,
            self.config.pool_max_size,
        )

    async def disconnect(self) -> None:
        """Close the pool and release all connections. Idempotent."""
        if self._pool is None:
            return
        logger.info("Closing database pool")
        await self._pool.close()
        self._pool = None

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator["asyncpg.Connection"]:  # type: ignore[name-defined]
        """Acquire a pooled connection as an async context manager."""
        async with self.pool.acquire() as connection:
            yield connection

    async def healthcheck(self) -> bool:
        """Return ``True`` if a trivial query succeeds, ``False`` otherwise."""
        if self._pool is None:
            return False
        try:
            async with self.acquire() as conn:
                await conn.execute("SELECT 1;")
            return True
        except Exception:
            logger.exception("Database healthcheck failed")
            return False


# Module-level singleton used by FastAPI dependencies. The app's lifespan owns
# its connect()/disconnect() calls (see api/app.py).
database = DatabaseManager()


def get_database() -> DatabaseManager:
    """FastAPI dependency provider returning the shared ``DatabaseManager``."""
    return database


__all__ = ["DatabaseConfig", "DatabaseManager", "database", "get_database"]
