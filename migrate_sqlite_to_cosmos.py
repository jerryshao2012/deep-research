#!/usr/bin/env python3
"""Migrate SQLite threads/runs from deep_research.db to Cosmos DB."""

import json
import os
import sqlite3
import subprocess

from azure.cosmos import CosmosClient, PartitionKey


def get_azure_credentials():
    print("🔐 Retrieving credentials from Key Vault...")
    kv_name = "kv-deep-agents-0312"

    endpoint = subprocess.check_output(
        ["az", "keyvault", "secret", "show", "--vault-name", kv_name, "--name", "COSMOSDB-ENDPOINT", "--query", "value",
         "-o", "tsv"],
        text=True
    ).strip()

    key = subprocess.check_output(
        ["az", "keyvault", "secret", "show", "--vault-name", kv_name, "--name", "COSMOSDB-KEY", "--query", "value",
         "-o", "tsv"],
        text=True
    ).strip()

    return endpoint, key


def migrate():
    endpoint, key = get_azure_credentials()

    db_path = "./deep_research.db"
    if not os.path.exists(db_path):
        print(f"❌ SQLite database not found at {db_path}!")
        return

    print(f"🔍 Reading thread and run data from SQLite database: {db_path}...")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 1. Fetch Threads
    cursor.execute("SELECT * FROM threads")
    threads = [dict(row) for row in cursor.fetchall()]

    # 2. Fetch Runs
    cursor.execute("SELECT * FROM runs")
    runs = [dict(row) for row in cursor.fetchall()]

    conn.close()

    print(f"✅ Found {len(threads)} threads and {len(runs)} runs in SQLite.")
    if not threads and not runs:
        print("ℹ️ Nothing to migrate.")
        return

    # Connect to Cosmos DB
    print("✨ Connecting to Cosmos DB...")
    client = CosmosClient(endpoint, credential=key)
    db_name = "deep-research-checkpoints"
    db_client = client.create_database_if_not_exists(id=db_name)

    threads_container = db_client.create_container_if_not_exists(
        id="threads", partition_key=PartitionKey(path="/id")
    )
    runs_container = db_client.create_container_if_not_exists(
        id="runs", partition_key=PartitionKey(path="/id")
    )

    # Migrate Threads
    print(f"📤 Migrating {len(threads)} threads to Cosmos DB...")
    for t in threads:
        thread_id = t["thread_id"]
        try:
            messages = json.loads(t["messages"])
        except Exception:
            messages = []

        try:
            values = json.loads(t["values_"])
        except Exception:
            values = {}

        try:
            metadata = json.loads(t["metadata"])
        except Exception:
            metadata = {}

        item = {
            "id": thread_id,
            "created_at": t["created_at"],
            "updated_at": t["updated_at"],
            "state_updated_at": t.get("state_updated_at") or t["created_at"],
            "messages": messages,
            "values": values,
            "metadata": metadata,
            "status": t["status"],
            "user_id": t["user_id"]
        }

        try:
            threads_container.upsert_item(body=item)
            print(f"  + Upserted thread: {thread_id}")
        except Exception as e:
            print(f"  ❌ Failed to upsert thread {thread_id}: {e}")

    # Migrate Runs
    print(f"📤 Migrating {len(runs)} runs to Cosmos DB...")
    for r in runs:
        run_id = r["run_id"]
        try:
            metadata = json.loads(r["metadata"])
        except Exception:
            metadata = {}

        try:
            kwargs = json.loads(r["kwargs"])
        except Exception:
            kwargs = {}

        item = {
            "id": run_id,
            "thread_id": r["thread_id"],
            "assistant_id": r["assistant_id"],
            "status": r["status"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "metadata": metadata,
            "kwargs": kwargs,
            "multitask_strategy": r["multitask_strategy"],
            "error": r.get("error")
        }

        try:
            runs_container.upsert_item(body=item)
            print(f"  + Upserted run: {run_id}")
        except Exception as e:
            print(f"  ❌ Failed to upsert run {run_id}: {e}")

    print("🎉 Migration completed successfully!")


if __name__ == "__main__":
    migrate()
