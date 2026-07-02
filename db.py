"""Database abstraction for threads/runs across SQLite, PostgreSQL, and CosmosDB."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from typing import Any

import db_sql

_sqlite_lock = threading.Lock()
_sqlite_conn = None
_postgres_pool = None
_cosmos_threads_container = None
_cosmos_runs_container = None


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 formatted string."""
    return datetime.now(UTC).isoformat()


def _safe_json_loads(value: Any, default: Any) -> Any:
    """Safely parse a JSON-encoded value, falling back to a default on failure.

    Handles the common case where database fields may be stored as JSON
    strings, already-parsed dicts/lists, or null.

    Args:
        value: The value to decode. May be a JSON string, dict, list, or None.
        default: Fallback value returned when ``value`` is None or an
            unparseable string.

    Returns:
        The parsed dict/list, or ``default`` if parsing fails.
    """
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return default


def _thread_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw database row into the canonical thread dictionary format.

    Extracts and parses JSON-encoded fields (messages, values, metadata)
    and normalizes field names for consistent downstream consumption.

    Args:
        row: A dictionary representing a database row with keys like
            ``thread_id``, ``created_at``, ``updated_at``, ``values``
            (or ``values_json``), ``messages``, ``metadata``, ``status``,
            and ``user_id``.

    Returns:
        A normalized thread dictionary with all JSON fields parsed.
    """
    messages = _safe_json_loads(row.get("messages"), [])
    values = _safe_json_loads(row.get("values") if "values" in row else row.get("values_json"), {})
    metadata = _safe_json_loads(row.get("metadata"), {})
    if "messages" not in values:
        values["messages"] = messages
    return {
        "thread_id": row["thread_id"],
        "created_at": row["created_at"],
        "updated_at": row.get("updated_at") or row["created_at"],
        "state_updated_at": row.get("state_updated_at"),
        "messages": messages,
        "values": values,
        "metadata": metadata,
        "status": row.get("status") or "idle",
        "user_id": row.get("user_id"),
    }


def _run_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw database row into the canonical run dictionary format.

    Args:
        row: A dictionary representing a database row with keys like
            ``run_id``, ``thread_id``, ``assistant_id``, ``status``,
            ``created_at``, ``updated_at``, ``metadata``, ``kwargs``,
            ``multitask_strategy``, and ``error``.

    Returns:
        A normalized run dictionary with JSON fields parsed.
    """
    return {
        "run_id": row["run_id"],
        "thread_id": row["thread_id"],
        "assistant_id": row["assistant_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row.get("updated_at") or row["created_at"],
        "metadata": _safe_json_loads(row.get("metadata"), {}),
        "kwargs": _safe_json_loads(row.get("kwargs"), {}),
        "multitask_strategy": row.get("multitask_strategy") or "enqueue",
        "error": row.get("error"),
    }


def _get_sqlite_conn():
    """Return the module-level SQLite connection, creating it lazily on first call.

    Configures WAL journal mode, a 30-second busy timeout, and row-based
    access for dictionary-like result rows.  Access is serialized through
    ``_sqlite_lock`` by callers, not internally.

    Returns:
        An open ``sqlite3.Connection`` with ``row_factory = sqlite3.Row``.
    """
    global _sqlite_conn
    if _sqlite_conn is None:
        import sqlite3

        db_path = os.environ.get("SQLITE_DB_PATH", ":memory:")
        _sqlite_conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
        _sqlite_conn.row_factory = sqlite3.Row
        try:
            _sqlite_conn.execute("PRAGMA journal_mode=WAL")
            _sqlite_conn.execute("PRAGMA busy_timeout=30000")
        except Exception:
            pass
    return _sqlite_conn


def _init_sqlite() -> None:
    """Initialize the SQLite database schema with migration support.

    Creates the ``threads`` and ``runs`` tables if they do not exist, then
    applies schema migrations for any missing columns (``updated_at``,
    ``state_updated_at``, ``metadata``, ``status``, ``kwargs``,
    ``multitask_strategy``) to support upgrades from older schemas.

    Silently warns on locking errors (common on CIFS network mounts) and
    attempts to continue with migrations.
    """
    try:
        with _sqlite_lock:
            conn = _get_sqlite_conn()
            conn.executescript(f"{db_sql.CREATE_THREADS_TABLE}{db_sql.CREATE_RUNS_TABLE}")

            thread_cols = {row[1] for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
            if "updated_at" not in thread_cols:
                conn.execute(db_sql.ALTER_THREADS_ADD_UPDATED_AT)
                conn.execute(db_sql.UPDATE_THREADS_SET_UPDATED_AT)
    except Exception as exc:
        print(f"⚠️ SQLite database initialization warning (locking on CIFS network mount?): {exc}")
        if "state_updated_at" not in thread_cols:
            conn.execute(db_sql.ALTER_THREADS_ADD_STATE_UPDATED_AT)
        if "metadata" not in thread_cols:
            conn.execute(db_sql.ALTER_THREADS_ADD_METADATA)
        if "status" not in thread_cols:
            conn.execute(db_sql.ALTER_THREADS_ADD_STATUS)

        run_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "updated_at" not in run_cols:
            conn.execute(db_sql.ALTER_RUNS_ADD_UPDATED_AT)
            conn.execute(db_sql.UPDATE_RUNS_SET_UPDATED_AT)
        if "metadata" not in run_cols:
            conn.execute(db_sql.ALTER_RUNS_ADD_METADATA)
        if "kwargs" not in run_cols:
            conn.execute(db_sql.ALTER_RUNS_ADD_KWARGS)
        if "multitask_strategy" not in run_cols:
            conn.execute(db_sql.ALTER_RUNS_ADD_MULTITASK_STRATEGY)

        conn.commit()


def _init_postgres() -> None:
    """Initialize the PostgreSQL connection pool and ensure the schema exists.

    Creates a ``psycopg_pool.ConnectionPool`` from ``DATABASE_URL``,
    ``POSTGRES_URL``, or individual ``POSTGRES_*`` environment variables.
    Then creates tables and applies schema migrations for missing columns.

    Raises:
        ImportError: If ``psycopg`` or ``psycopg_pool`` is not installed.
    """
    global _postgres_pool
    if _postgres_pool is None:
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise ImportError(
                "PostgreSQL driver missing. Install psycopg and psycopg_pool."
            ) from exc

        pg_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
        if pg_url:
            _postgres_pool = ConnectionPool(conninfo=pg_url, min_size=2, max_size=10, open=True)
        else:
            host = os.environ.get("POSTGRES_HOST", "localhost")
            port = os.environ.get("POSTGRES_PORT", "5432")
            user = os.environ.get("POSTGRES_USER", "postgres")
            password = os.environ.get("POSTGRES_PASSWORD", "")
            dbname = os.environ.get("POSTGRES_DB", "postgres")
            conninfo = f"host={host} port={port} user={user} password={password} dbname={dbname}"
            _postgres_pool = ConnectionPool(conninfo=conninfo, min_size=2, max_size=10, open=True)

    with _postgres_pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(db_sql.CREATE_THREADS_TABLE)
            cur.execute(db_sql.CREATE_RUNS_TABLE)
            cur.execute(db_sql.ALTER_THREADS_ADD_UPDATED_AT_IF_NOT_EXISTS)
            cur.execute(db_sql.UPDATE_THREADS_SET_UPDATED_AT)
            cur.execute(db_sql.ALTER_THREADS_ADD_STATE_UPDATED_AT_IF_NOT_EXISTS)
            cur.execute(db_sql.ALTER_THREADS_ADD_METADATA_IF_NOT_EXISTS)
            cur.execute(db_sql.ALTER_THREADS_ADD_STATUS_IF_NOT_EXISTS)
            cur.execute(db_sql.ALTER_RUNS_ADD_UPDATED_AT_IF_NOT_EXISTS)
            cur.execute(db_sql.UPDATE_RUNS_SET_UPDATED_AT)
            cur.execute(db_sql.ALTER_RUNS_ADD_METADATA_IF_NOT_EXISTS)
            cur.execute(db_sql.ALTER_RUNS_ADD_KWARGS_IF_NOT_EXISTS)
            cur.execute(db_sql.ALTER_RUNS_ADD_MULTITASK_STRATEGY_IF_NOT_EXISTS)


def _init_cosmos_db() -> None:
    """Initialize Azure CosmosDB containers for threads and runs.

    Creates the database and ``threads``/``runs`` containers if they do not
    already exist, partitioned by ``/id``.  Supports authentication via
    connection string (``COSMOS_CONNECTION_STRING``) or endpoint + key
    (``COSMOSDB_ENDPOINT``, ``COSMOSDB_KEY``).

    Raises:
        ImportError: If ``azure-cosmos`` is not installed.
        ValueError: If no CosmosDB credentials are configured.
    """
    global _cosmos_threads_container, _cosmos_runs_container
    if _cosmos_threads_container is not None:
        return

    try:
        from azure.cosmos import CosmosClient, PartitionKey
    except ImportError as exc:
        raise ImportError("Azure Cosmos SDK missing. Install azure-cosmos.") from exc

    endpoint = os.environ.get("COSMOSDB_ENDPOINT")
    key = os.environ.get("COSMOSDB_KEY")
    conn_str = os.environ.get("COSMOS_CONNECTION_STRING")
    db_name = os.environ.get("COSMOSDB_DB_NAME", "deep_research")

    if conn_str:
        client = CosmosClient.from_connection_string(conn_str)
    elif endpoint and key:
        client = CosmosClient(endpoint, credential=key)
    else:
        raise ValueError("Cosmos configuration missing.")

    db_client = client.create_database_if_not_exists(id=db_name)
    _cosmos_threads_container = db_client.create_container_if_not_exists(
        id="threads", partition_key=PartitionKey(path="/id")
    )
    _cosmos_runs_container = db_client.create_container_if_not_exists(
        id="runs", partition_key=PartitionKey(path="/id")
    )


def init_db() -> None:
    """Initialize the database based on the ``DB_TYPE`` environment variable.

    Dispatches to the appropriate backend-specific initializer:
    ``sqlite`` (default), ``postgres``, or ``cosmosdb``.

    Raises:
        ValueError: If ``DB_TYPE`` is set to an unsupported value.
    """
    db_type = os.environ.get("DB_TYPE", "sqlite").lower()
    if db_type == "sqlite":
        _init_sqlite()
    elif db_type == "postgres":
        _init_postgres()
    elif db_type == "cosmosdb":
        _init_cosmos_db()
    else:
        raise ValueError(f"Unsupported DB_TYPE: {db_type}")


def get_thread(thread_id: str) -> dict[str, Any] | None:
    """Retrieve a single thread by its ID.

    Args:
        thread_id: The unique thread identifier.

    Returns:
        A normalized thread dictionary, or ``None`` if no thread with the
        given ID exists.
    """
    db_type = os.environ.get("DB_TYPE", "sqlite").lower()
    if db_type == "sqlite":
        with _sqlite_lock:
            conn = _get_sqlite_conn()
            row = conn.execute(db_sql.GET_THREAD_BY_ID_SQLITE, (thread_id,)).fetchone()
        if row is None:
            return None
        return _thread_to_dict(dict(row))

    if db_type == "postgres":
        from psycopg.rows import dict_row

        with _postgres_pool.connection() as conn:
            conn.row_factory = dict_row
            with conn.cursor() as cur:
                cur.execute(db_sql.GET_THREAD_BY_ID_POSTGRES, (thread_id,))
                row = cur.fetchone()
        if row is None:
            return None
        return _thread_to_dict(dict(row))

    try:
        item = _cosmos_threads_container.read_item(item=thread_id, partition_key=thread_id)
    except Exception:
        return None

    return _thread_to_dict(
        {
            "thread_id": item["id"],
            "created_at": item["created_at"],
            "updated_at": item.get("updated_at"),
            "state_updated_at": item.get("state_updated_at"),
            "messages": item.get("messages", []),
            "values": item.get("values", {}),
            "metadata": item.get("metadata", {}),
            "status": item.get("status", "idle"),
            "user_id": item.get("user_id"),
        }
    )


def create_thread(
        thread_id: str,
        user_id: str,
        created_at: str,
        metadata: dict[str, Any] | None = None,
        status: str = "idle",
        values: dict[str, Any] | None = None,
) -> None:
    """Insert a new thread record into the database.

    Args:
        thread_id: The unique thread identifier.
        user_id: The owner of the thread.
        created_at: ISO 8601 creation timestamp.
        metadata: Optional key-value metadata. Defaults to empty dict.
        status: Thread status string. Defaults to ``"idle"``.
        values: Optional initial state values dict. An empty ``messages``
            list is set by default.
    """
    metadata = metadata or {}
    values = dict(values or {})
    values.setdefault("messages", [])

    db_type = os.environ.get("DB_TYPE", "sqlite").lower()
    if db_type == "sqlite":
        with _sqlite_lock:
            conn = _get_sqlite_conn()
            conn.execute(
                db_sql.INSERT_THREAD_SQLITE,
                (
                    thread_id,
                    created_at,
                    created_at,
                    created_at,
                    json.dumps(values.get("messages", [])),
                    json.dumps(values),
                    json.dumps(metadata),
                    status,
                    user_id,
                ),
            )
            conn.commit()
        return

    if db_type == "postgres":
        with _postgres_pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    db_sql.INSERT_THREAD_POSTGRES,
                    (
                        thread_id,
                        created_at,
                        created_at,
                        created_at,
                        json.dumps(values.get("messages", [])),
                        json.dumps(values),
                        json.dumps(metadata),
                        status,
                        user_id,
                    ),
                )
        return

    _cosmos_threads_container.create_item(
        body={
            "id": thread_id,
            "created_at": created_at,
            "updated_at": created_at,
            "state_updated_at": created_at,
            "messages": values.get("messages", []),
            "values": values,
            "metadata": metadata,
            "status": status,
            "user_id": user_id,
        }
    )


def update_thread(
        thread_id: str,
        messages: list,
        values: dict,
        metadata: dict[str, Any] | None = None,
        status: str | None = None,
        updated_at: str | None = None,
) -> None:
    """Update a thread's messages, values, metadata, and status.

    If the thread does not exist, the call is a no-op.  When ``metadata``
    is ``None``, the existing metadata is preserved; otherwise it is
    replaced.

    Args:
        thread_id: The unique thread identifier.
        messages: The full message list to store.
        values: State values dict.  ``values["messages"]`` is set to the
            ``messages`` argument.
        metadata: If provided, replaces the existing metadata. If ``None``,
            the current metadata is kept.
        status: New status string. Defaults to the current status or
            ``"idle"``.
        updated_at: ISO 8601 timestamp. Defaults to the current UTC time.
    """
    current = get_thread(thread_id)
    if current is None:
        return

    next_values = dict(values or {})
    next_values["messages"] = messages
    next_metadata = current.get("metadata", {}) if metadata is None else metadata
    next_status = status or current.get("status") or "idle"
    now = updated_at or _now_iso()

    db_type = os.environ.get("DB_TYPE", "sqlite").lower()
    if db_type == "sqlite":
        with _sqlite_lock:
            conn = _get_sqlite_conn()
            conn.execute(
                db_sql.UPDATE_THREAD_SQLITE,
                (
                    json.dumps(messages),
                    json.dumps(next_values),
                    json.dumps(next_metadata),
                    next_status,
                    now,
                    now,
                    thread_id,
                ),
            )
            conn.commit()
        return

    if db_type == "postgres":
        with _postgres_pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    db_sql.UPDATE_THREAD_POSTGRES,
                    (
                        json.dumps(messages),
                        json.dumps(next_values),
                        json.dumps(next_metadata),
                        next_status,
                        now,
                        now,
                        thread_id,
                    ),
                )
        return

    item = _cosmos_threads_container.read_item(item=thread_id, partition_key=thread_id)
    item["messages"] = messages
    item["values"] = next_values
    item["metadata"] = next_metadata
    item["status"] = next_status
    item["updated_at"] = now
    item["state_updated_at"] = now
    _cosmos_threads_container.upsert_item(body=item)


def update_thread_metadata(thread_id: str, metadata: dict[str, Any]) -> bool:
    """Merge metadata keys into a thread's existing metadata.

    Args:
        thread_id: The unique thread identifier.
        metadata: Key-value pairs to merge on top of the existing metadata.

    Returns:
        ``True`` if the thread exists and metadata was updated, ``False``
        if the thread was not found.
    """
    thread = get_thread(thread_id)
    if thread is None:
        return False

    merged = dict(thread.get("metadata") or {})
    merged.update(metadata or {})
    now = _now_iso()

    db_type = os.environ.get("DB_TYPE", "sqlite").lower()
    if db_type == "sqlite":
        with _sqlite_lock:
            conn = _get_sqlite_conn()
            conn.execute(db_sql.UPDATE_THREAD_METADATA_SQLITE, (json.dumps(merged), now, thread_id))
            conn.commit()
    elif db_type == "postgres":
        with _postgres_pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(db_sql.UPDATE_THREAD_METADATA_POSTGRES, (json.dumps(merged), now, thread_id))
    else:
        item = _cosmos_threads_container.read_item(item=thread_id, partition_key=thread_id)
        item["metadata"] = merged
        item["updated_at"] = now
        _cosmos_threads_container.upsert_item(body=item)

    return True


def update_thread_state(thread_id: str, values: dict[str, Any]) -> bool:
    """Merge state values into a thread, preserving existing messages.

    Args:
        thread_id: The unique thread identifier.
        values: State key-value pairs to merge.

    Returns:
        ``True`` if the thread exists and state was updated, ``False``
        if the thread was not found.
    """
    thread = get_thread(thread_id)
    if thread is None:
        return False

    merged_values = dict(thread.get("values") or {})
    merged_values.update(values or {})
    messages = merged_values.get("messages") or thread.get("messages") or []
    update_thread(
        thread_id,
        messages,
        merged_values,
        metadata=thread.get("metadata") or {},
        status=thread.get("status") or "idle",
    )
    return True


def delete_thread(thread_id: str) -> bool:
    """Delete a thread and all its associated runs.

    Args:
        thread_id: The unique thread identifier.

    Returns:
        ``True`` if the thread existed and was deleted, ``False`` if it
        was not found.
    """
    thread = get_thread(thread_id)
    if thread is None:
        return False

    db_type = os.environ.get("DB_TYPE", "sqlite").lower()
    if db_type == "sqlite":
        with _sqlite_lock:
            conn = _get_sqlite_conn()
            conn.execute(db_sql.DELETE_RUNS_BY_THREAD_ID_SQLITE, (thread_id,))
            conn.execute(db_sql.DELETE_THREAD_BY_ID_SQLITE, (thread_id,))
            conn.commit()
    elif db_type == "postgres":
        with _postgres_pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(db_sql.DELETE_RUNS_BY_THREAD_ID_POSTGRES, (thread_id,))
                cur.execute(db_sql.DELETE_THREAD_BY_ID_POSTGRES, (thread_id,))
    else:
        _cosmos_threads_container.delete_item(item=thread_id, partition_key=thread_id)
        parameters = [{"name": "@thread_id", "value": thread_id}]
        for item in _cosmos_runs_container.query_items(
                query=db_sql.COSMOS_GET_RUN_IDS_BY_THREAD_ID,
                parameters=parameters,
                enable_cross_partition_query=True,
        ):
            _cosmos_runs_container.delete_item(item=item["id"], partition_key=item["id"])

    return True


def search_threads(
        *,
        limit: int = 10,
        offset: int = 0,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Search and filter threads with pagination and sorting.

    Args:
        limit: Maximum number of threads to return (1–1000). Defaults to 10.
        offset: Number of threads to skip for pagination. Defaults to 0.
        sort_by: Column to sort by. Must be one of ``"thread_id"``,
            ``"status"``, ``"created_at"``, ``"updated_at"``, or
            ``"state_updated_at"``. Defaults to ``"updated_at"``.
        sort_order: ``"asc"`` or ``"desc"``. Defaults to ``"desc"``.
        status: Optional status filter (exact match).
        metadata: Optional metadata key-value filters (exact match on each
            key). Only threads whose metadata contains all specified
            key-value pairs are returned.
        user_id: Optional user filter.

    Returns:
        A list of normalized thread dictionaries matching all filters.
    """
    limit = max(1, min(int(limit or 10), 1000))
    offset = max(0, int(offset or 0))
    sort_by = sort_by if sort_by in {"thread_id", "status", "created_at", "updated_at",
                                     "state_updated_at"} else "updated_at"
    sort_order = "asc" if (sort_order or "").lower() == "asc" else "desc"
    metadata = metadata or {}

    db_type = os.environ.get("DB_TYPE", "sqlite").lower()
    items: list[dict[str, Any]]

    if db_type == "sqlite":
        with _sqlite_lock:
            conn = _get_sqlite_conn()
            rows = conn.execute(f"{db_sql.SEARCH_THREADS_BASE_SQL} ORDER BY {sort_by} {sort_order}").fetchall()
        items = [_thread_to_dict(dict(row)) for row in rows]
    elif db_type == "postgres":
        from psycopg.rows import dict_row

        with _postgres_pool.connection() as conn:
            conn.row_factory = dict_row
            with conn.cursor() as cur:
                cur.execute(f"{db_sql.SEARCH_THREADS_BASE_SQL} ORDER BY {sort_by} {sort_order}")
                rows = cur.fetchall()
        items = [_thread_to_dict(dict(row)) for row in rows]
    else:
        rows = list(_cosmos_threads_container.query_items(query=db_sql.COSMOS_SEARCH_THREADS,
                                                          enable_cross_partition_query=True))
        items = [
            _thread_to_dict(
                {
                    "thread_id": item["id"],
                    "created_at": item["created_at"],
                    "updated_at": item.get("updated_at"),
                    "state_updated_at": item.get("state_updated_at"),
                    "messages": item.get("messages", []),
                    "values": item.get("values", {}),
                    "metadata": item.get("metadata", {}),
                    "status": item.get("status", "idle"),
                    "user_id": item.get("user_id"),
                }
            )
            for item in rows
        ]
        items.sort(key=lambda x: x.get(sort_by) or "", reverse=(sort_order == "desc"))

    def _match(item: dict[str, Any]) -> bool:
        """Check if a thread item matches all filter criteria."""
        if status is not None and item.get("status") != status:
            return False
        if user_id and item.get("user_id") not in {None, "", user_id}:
            return False
        existing_meta = item.get("metadata") or {}
        for k, v in metadata.items():
            if existing_meta.get(k) != v:
                return False
        return True

    filtered = [item for item in items if _match(item)]
    return filtered[offset: offset + limit]


def get_run(run_id: str) -> dict[str, Any] | None:
    """Retrieve a single run by its ID.

    Args:
        run_id: The unique run identifier.

    Returns:
        A normalized run dictionary, or ``None`` if no run with the given
        ID exists.
    """
    db_type = os.environ.get("DB_TYPE", "sqlite").lower()
    if db_type == "sqlite":
        with _sqlite_lock:
            conn = _get_sqlite_conn()
            row = conn.execute(db_sql.GET_RUN_BY_ID_SQLITE, (run_id,)).fetchone()
        if row is None:
            return None
        return _run_to_dict(dict(row))

    if db_type == "postgres":
        from psycopg.rows import dict_row

        with _postgres_pool.connection() as conn:
            conn.row_factory = dict_row
            with conn.cursor() as cur:
                cur.execute(db_sql.GET_RUN_BY_ID_POSTGRES, (run_id,))
                row = cur.fetchone()
        if row is None:
            return None
        return _run_to_dict(dict(row))

    try:
        item = _cosmos_runs_container.read_item(item=run_id, partition_key=run_id)
    except Exception:
        return None

    return _run_to_dict(
        {
            "run_id": item["id"],
            "thread_id": item["thread_id"],
            "assistant_id": item["assistant_id"],
            "status": item["status"],
            "created_at": item["created_at"],
            "updated_at": item.get("updated_at"),
            "metadata": item.get("metadata", {}),
            "kwargs": item.get("kwargs", {}),
            "multitask_strategy": item.get("multitask_strategy", "enqueue"),
            "error": item.get("error"),
        }
    )


def list_runs(thread_id: str, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
    """List runs for a thread, ordered by creation time (newest first).

    Args:
        thread_id: The thread whose runs to list.
        limit: Maximum number of runs to return (1–1000). Defaults to 10.
        offset: Number of runs to skip for pagination. Defaults to 0.

    Returns:
        A list of normalized run dictionaries.
    """
    limit = max(1, min(int(limit or 10), 1000))
    offset = max(0, int(offset or 0))
    db_type = os.environ.get("DB_TYPE", "sqlite").lower()

    if db_type == "sqlite":
        with _sqlite_lock:
            conn = _get_sqlite_conn()
            rows = conn.execute(db_sql.LIST_RUNS_BY_THREAD_ID_SQLITE, (thread_id, limit, offset)).fetchall()
        return [_run_to_dict(dict(row)) for row in rows]

    if db_type == "postgres":
        from psycopg.rows import dict_row

        with _postgres_pool.connection() as conn:
            conn.row_factory = dict_row
            with conn.cursor() as cur:
                cur.execute(db_sql.LIST_RUNS_BY_THREAD_ID_POSTGRES, (thread_id, limit, offset))
                rows = cur.fetchall()
        return [_run_to_dict(dict(row)) for row in rows]

    items = list(
        _cosmos_runs_container.query_items(
            query=db_sql.COSMOS_LIST_RUNS_BY_THREAD_ID,
            parameters=[{"name": "@thread_id", "value": thread_id}],
            enable_cross_partition_query=True,
        )
    )
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return [
        _run_to_dict(
            {
                "run_id": item["id"],
                "thread_id": item["thread_id"],
                "assistant_id": item["assistant_id"],
                "status": item["status"],
                "created_at": item["created_at"],
                "updated_at": item.get("updated_at"),
                "metadata": item.get("metadata", {}),
                "kwargs": item.get("kwargs", {}),
                "multitask_strategy": item.get("multitask_strategy", "enqueue"),
                "error": item.get("error"),
            }
        )
        for item in items[offset: offset + limit]
    ]


def create_run(
        run_id: str,
        thread_id: str,
        assistant_id: str,
        created_at: str,
        multitask_strategy: str | None = None,
) -> None:
    """Insert a new run record with status ``"pending"``.

    Args:
        run_id: The unique run identifier.
        thread_id: The thread this run belongs to.
        assistant_id: The assistant configuration ID.
        created_at: ISO 8601 creation timestamp.
        multitask_strategy: Queueing strategy. Defaults to ``"enqueue"``.
    """
    strategy = multitask_strategy or "enqueue"
    db_type = os.environ.get("DB_TYPE", "sqlite").lower()

    if db_type == "sqlite":
        with _sqlite_lock:
            conn = _get_sqlite_conn()
            conn.execute(
                db_sql.INSERT_RUN_SQLITE,
                (
                    run_id,
                    thread_id,
                    assistant_id,
                    "pending",
                    created_at,
                    created_at,
                    json.dumps({}),
                    json.dumps({}),
                    strategy,
                    None,
                ),
            )
            conn.commit()
        return

    if db_type == "postgres":
        with _postgres_pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    db_sql.INSERT_RUN_POSTGRES,
                    (
                        run_id,
                        thread_id,
                        assistant_id,
                        "pending",
                        created_at,
                        created_at,
                        json.dumps({}),
                        json.dumps({}),
                        strategy,
                        None,
                    ),
                )
        return

    _cosmos_runs_container.create_item(
        body={
            "id": run_id,
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "status": "pending",
            "created_at": created_at,
            "updated_at": created_at,
            "metadata": {},
            "kwargs": {},
            "multitask_strategy": strategy,
            "error": None,
        }
    )


def update_run_status(run_id: str, status: str, error: str | None = None) -> None:
    """Update a run's status and optional error message.

    Args:
        run_id: The unique run identifier.
        status: New status string (e.g., ``"pending"``, ``"running"``,
            ``"success"``, ``"error"``, ``"cancelled"``).
        error: Optional error description. Only written when not ``None``.
    """
    now = _now_iso()
    db_type = os.environ.get("DB_TYPE", "sqlite").lower()

    if db_type == "sqlite":
        with _sqlite_lock:
            conn = _get_sqlite_conn()
            conn.execute(db_sql.UPDATE_RUN_STATUS_SQLITE, (status, error, now, run_id))
            conn.commit()
        return

    if db_type == "postgres":
        with _postgres_pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(db_sql.UPDATE_RUN_STATUS_POSTGRES, (status, error, now, run_id))
        return

    item = _cosmos_runs_container.read_item(item=run_id, partition_key=run_id)
    item["status"] = status
    item["updated_at"] = now
    if error is not None:
        item["error"] = error
    _cosmos_runs_container.upsert_item(body=item)


def cancel_running_runs(thread_id: str) -> None:
    """Cancel all pending or running runs for a given thread.

    Sets the status of every non-terminal run belonging to ``thread_id``
    to ``"cancelled"``.

    Args:
        thread_id: The thread whose active runs should be cancelled.
    """
    db_type = os.environ.get("DB_TYPE", "sqlite").lower()

    if db_type == "sqlite":
        with _sqlite_lock:
            conn = _get_sqlite_conn()
            conn.execute(db_sql.CANCEL_RUNNING_RUNS_SQLITE, (_now_iso(), thread_id))
            conn.commit()
        return

    if db_type == "postgres":
        with _postgres_pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(db_sql.CANCEL_RUNNING_RUNS_POSTGRES, (_now_iso(), thread_id))
        return

    items = list(
        _cosmos_runs_container.query_items(
            query=db_sql.COSMOS_CANCEL_RUNNING_RUNS,
            parameters=[{"name": "@thread_id", "value": thread_id}],
            enable_cross_partition_query=True,
        )
    )
    for item in items:
        item["status"] = "cancelled"
        item["updated_at"] = _now_iso()
        _cosmos_runs_container.upsert_item(body=item)
