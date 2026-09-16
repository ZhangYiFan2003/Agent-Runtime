from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from axiom.config import StorageConfig
from axiom.runtime.checkpoints import RuntimeStore, SQLiteCheckpointStore
from axiom.runtime.control_plane import ControlOperationStore, SQLiteControlOperationStore
from axiom.runtime.events import EventRepository, ThreadEventRepository


@dataclass(slots=True)
class DurableStorage:
    """The repositories that jointly hold durable Runtime truth."""

    backend: str
    runtime: RuntimeStore
    events: EventRepository
    controls: ControlOperationStore
    _close: object | None = None

    def close(self) -> None:
        close = getattr(self._close, "close", None)
        if close is not None:
            close()


def create_durable_storage(
    config: StorageConfig,
    *,
    default_sqlite_path: str | Path,
) -> DurableStorage:
    backend = config.backend.strip().lower()
    if backend == "sqlite":
        path = Path(config.sqlite_path).expanduser() if config.sqlite_path else Path(
            default_sqlite_path
        ).expanduser()
        return DurableStorage(
            backend="sqlite",
            runtime=SQLiteCheckpointStore(path),
            events=ThreadEventRepository(path),
            controls=SQLiteControlOperationStore(path),
        )
    if backend == "postgres":
        from axiom.runtime.postgres import (
            PostgresConnectionPool,
            PostgresControlOperationStore,
            PostgresEventRepository,
            PostgresRuntimeStore,
            initialize_postgres_schema,
        )

        pool = PostgresConnectionPool(
            config.postgres_dsn,
            min_size=config.pool_min_size,
            max_size=config.pool_max_size,
            connect_timeout_seconds=config.connect_timeout_seconds,
        )
        try:
            initialize_postgres_schema(pool)
        except Exception:
            pool.close()
            raise
        return DurableStorage(
            backend="postgres",
            runtime=PostgresRuntimeStore(pool),
            events=PostgresEventRepository(pool),
            controls=PostgresControlOperationStore(pool),
            _close=pool,
        )
    raise ValueError(f"unsupported storage backend: {config.backend}")
