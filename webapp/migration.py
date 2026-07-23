import json
import os
import sqlite3


async def migrate_threads_to_checkpointer() -> None:
    """Migrate thread IDs from deep_research.db into LangGraph Platform checkpointer."""
    try:
        mem_type = os.environ.get("MEMORY_TYPE", "").strip().lower()
        if mem_type in ("sqlite", "postgres", "postgresql"):
            print(
                f"ℹ️  [Thread Migration] MEMORY_TYPE={mem_type} — checkpointer handles its own storage, skipping migration")
            return

        db_path = os.environ.get("SQLITE_DB_PATH", "/deps/deep_research/deep_research.db")
        if not os.path.exists(db_path):
            print(f"⚠️  [Thread Migration] deep_research.db not found at {db_path} — skipping migration")
            return

        print(f"🔍 [Thread Migration] Reading thread IDs from {db_path}...")
        conn_db = sqlite3.connect(db_path)
        cursor = conn_db.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='threads';")
        if not cursor.fetchone():
            print("⚠️  [Thread Migration] Table 'threads' does not exist in deep_research.db — skipping migration")
            conn_db.close()
            return

        cursor.execute("SELECT thread_id, metadata, user_id FROM threads;")
        rows = cursor.fetchall()
        conn_db.close()

        if not rows:
            print("ℹ️  [Thread Migration] No threads found in deep_research.db")
            return

        print(
            f"📦 [Thread Migration] Found {len(rows)} threads in deep_research.db. Syncing to LangGraph checkpointer...")

        from langgraph_runtime.database import connect
        from langgraph_runtime.ops import Threads

        async with connect(supports_core_api=False) as conn:
            for thread_id, metadata_json, user_id in rows:
                try:
                    metadata = json.loads(metadata_json) if metadata_json else {}
                except Exception:
                    metadata = {}

                # Ensure owner/user_id is in metadata for auth
                if user_id and "owner" not in metadata:
                    metadata["owner"] = user_id

                try:
                    # Put thread with on conflict behavior
                    await Threads.put(
                        conn,
                        thread_id,
                        metadata=metadata,
                        if_exists="do_nothing",
                    )
                    print(f"   ✅ Registered thread in checkpointer: {thread_id}")
                except Exception as e:
                    # Fallback to raising if conflict behavior mismatch
                    try:
                        await Threads.put(
                            conn,
                            thread_id,
                            metadata=metadata,
                            if_exists="raise",
                        )
                        print(f"   ✅ Registered thread in checkpointer (fallback): {thread_id}")
                    except Exception as fallback_err:
                        print(f"   ⚠️  Failed to register thread {thread_id}: {fallback_err}")

        print("✅ [Thread Migration] Thread sync/migration complete!")
    except Exception as exc:
        print(f"⚠️  [Thread Migration] Failed to sync threads to checkpointer: {exc}")
        import traceback
        traceback.print_exc()
