"""SQL statement constants for the database module."""

# ── Schema Creation ───────────────────────────────────────────────────────────

CREATE_THREADS_TABLE = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    state_updated_at TEXT,
    messages TEXT NOT NULL DEFAULT '[]',
    values_ TEXT NOT NULL DEFAULT '{}',
    metadata TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'idle',
    user_id TEXT
);
"""

CREATE_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(thread_id),
    assistant_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    kwargs TEXT NOT NULL DEFAULT '{}',
    multitask_strategy TEXT NOT NULL DEFAULT 'enqueue',
    error TEXT
);
"""

# ── Schema Migration (SQLite) ─────────────────────────────────────────────────

ALTER_THREADS_ADD_UPDATED_AT = "ALTER TABLE threads ADD COLUMN updated_at TEXT"
UPDATE_THREADS_SET_UPDATED_AT = "UPDATE threads SET updated_at = created_at WHERE updated_at IS NULL"
ALTER_THREADS_ADD_STATE_UPDATED_AT = "ALTER TABLE threads ADD COLUMN state_updated_at TEXT"
ALTER_THREADS_ADD_METADATA = "ALTER TABLE threads ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'"
ALTER_THREADS_ADD_STATUS = "ALTER TABLE threads ADD COLUMN status TEXT NOT NULL DEFAULT 'idle'"

ALTER_RUNS_ADD_UPDATED_AT = "ALTER TABLE runs ADD COLUMN updated_at TEXT"
UPDATE_RUNS_SET_UPDATED_AT = "UPDATE runs SET updated_at = created_at WHERE updated_at IS NULL"
ALTER_RUNS_ADD_METADATA = "ALTER TABLE runs ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'"
ALTER_RUNS_ADD_KWARGS = "ALTER TABLE runs ADD COLUMN kwargs TEXT NOT NULL DEFAULT '{}'"
ALTER_RUNS_ADD_MULTITASK_STRATEGY = "ALTER TABLE runs ADD COLUMN multitask_strategy TEXT NOT NULL DEFAULT 'enqueue'"

# ── Schema Migration (PostgreSQL) ─────────────────────────────────────────────

ALTER_THREADS_ADD_UPDATED_AT_IF_NOT_EXISTS = "ALTER TABLE threads ADD COLUMN IF NOT EXISTS updated_at TEXT"
ALTER_THREADS_ADD_STATE_UPDATED_AT_IF_NOT_EXISTS = "ALTER TABLE threads ADD COLUMN IF NOT EXISTS state_updated_at TEXT"
ALTER_THREADS_ADD_METADATA_IF_NOT_EXISTS = "ALTER TABLE threads ADD COLUMN IF NOT EXISTS metadata TEXT NOT NULL DEFAULT '{}'"
ALTER_THREADS_ADD_STATUS_IF_NOT_EXISTS = "ALTER TABLE threads ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'idle'"

ALTER_RUNS_ADD_UPDATED_AT_IF_NOT_EXISTS = "ALTER TABLE runs ADD COLUMN IF NOT EXISTS updated_at TEXT"
ALTER_RUNS_ADD_METADATA_IF_NOT_EXISTS = "ALTER TABLE runs ADD COLUMN IF NOT EXISTS metadata TEXT NOT NULL DEFAULT '{}'"
ALTER_RUNS_ADD_KWARGS_IF_NOT_EXISTS = "ALTER TABLE runs ADD COLUMN IF NOT EXISTS kwargs TEXT NOT NULL DEFAULT '{}'"
ALTER_RUNS_ADD_MULTITASK_STRATEGY_IF_NOT_EXISTS = "ALTER TABLE runs ADD COLUMN IF NOT EXISTS multitask_strategy TEXT NOT NULL DEFAULT 'enqueue'"

# ── Thread Operations ─────────────────────────────────────────────────────────

GET_THREAD_BY_ID_SQLITE = """
    SELECT thread_id, created_at, updated_at, state_updated_at, messages, values_ AS values_json, metadata, status, user_id
    FROM threads WHERE thread_id = ?
"""
GET_THREAD_BY_ID_POSTGRES = """
    SELECT thread_id, created_at, updated_at, state_updated_at, messages, values_ AS values_json, metadata, status, user_id
    FROM threads WHERE thread_id = %s
"""

INSERT_THREAD_SQLITE = """
    INSERT INTO threads (thread_id, created_at, updated_at, state_updated_at, messages, values_, metadata, status, user_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
INSERT_THREAD_POSTGRES = """
    INSERT INTO threads (thread_id, created_at, updated_at, state_updated_at, messages, values_, metadata, status, user_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

UPDATE_THREAD_SQLITE = """
    UPDATE threads SET messages = ?, values_ = ?, metadata = ?, status = ?, updated_at = ?, state_updated_at = ?
    WHERE thread_id = ?
"""
UPDATE_THREAD_POSTGRES = """
    UPDATE threads SET messages = %s, values_ = %s, metadata = %s, status = %s, updated_at = %s, state_updated_at = %s
    WHERE thread_id = %s
"""

UPDATE_THREAD_METADATA_SQLITE = "UPDATE threads SET metadata = ?, updated_at = ? WHERE thread_id = ?"
UPDATE_THREAD_METADATA_POSTGRES = "UPDATE threads SET metadata = %s, updated_at = %s WHERE thread_id = %s"

DELETE_RUNS_BY_THREAD_ID_SQLITE = "DELETE FROM runs WHERE thread_id = ?"
DELETE_THREAD_BY_ID_SQLITE = "DELETE FROM threads WHERE thread_id = ?"
DELETE_RUNS_BY_THREAD_ID_POSTGRES = "DELETE FROM runs WHERE thread_id = %s"
DELETE_THREAD_BY_ID_POSTGRES = "DELETE FROM threads WHERE thread_id = %s"

SEARCH_THREADS_BASE_SQL = """
    SELECT thread_id, created_at, updated_at, state_updated_at, messages, values_ AS values_json, metadata, status, user_id
    FROM threads
"""

# ── Run Operations ────────────────────────────────────────────────────────────

GET_RUN_BY_ID_SQLITE = """
    SELECT run_id, thread_id, assistant_id, status, created_at, updated_at, metadata, kwargs, multitask_strategy, error
    FROM runs WHERE run_id = ?
"""
GET_RUN_BY_ID_POSTGRES = """
    SELECT run_id, thread_id, assistant_id, status, created_at, updated_at, metadata, kwargs, multitask_strategy, error
    FROM runs WHERE run_id = %s
"""

LIST_RUNS_BY_THREAD_ID_SQLITE = """
    SELECT run_id, thread_id, assistant_id, status, created_at, updated_at, metadata, kwargs, multitask_strategy, error
    FROM runs WHERE thread_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?
"""
LIST_RUNS_BY_THREAD_ID_POSTGRES = """
    SELECT run_id, thread_id, assistant_id, status, created_at, updated_at, metadata, kwargs, multitask_strategy, error
    FROM runs WHERE thread_id = %s ORDER BY created_at DESC LIMIT %s OFFSET %s
"""

INSERT_RUN_SQLITE = """
    INSERT INTO runs (run_id, thread_id, assistant_id, status, created_at, updated_at, metadata, kwargs, multitask_strategy, error)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
INSERT_RUN_POSTGRES = """
    INSERT INTO runs (run_id, thread_id, assistant_id, status, created_at, updated_at, metadata, kwargs, multitask_strategy, error)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

UPDATE_RUN_STATUS_SQLITE = "UPDATE runs SET status = ?, error = ?, updated_at = ? WHERE run_id = ?"
UPDATE_RUN_STATUS_POSTGRES = "UPDATE runs SET status = %s, error = %s, updated_at = %s WHERE run_id = %s"

CANCEL_RUNNING_RUNS_SQLITE = "UPDATE runs SET status = 'cancelled', updated_at = ? WHERE thread_id = ? AND status = 'running'"
CANCEL_RUNNING_RUNS_POSTGRES = "UPDATE runs SET status = 'cancelled', updated_at = %s WHERE thread_id = %s AND status = 'running'"

# ── Cosmos DB Queries ─────────────────────────────────────────────────────────

COSMOS_GET_RUN_IDS_BY_THREAD_ID = "SELECT c.id FROM c WHERE c.thread_id = @thread_id"
COSMOS_SEARCH_THREADS = "SELECT * FROM c"
COSMOS_LIST_RUNS_BY_THREAD_ID = "SELECT * FROM c WHERE c.thread_id = @thread_id"
COSMOS_CANCEL_RUNNING_RUNS = "SELECT * FROM c WHERE c.thread_id = @thread_id AND c.status = 'running'"
