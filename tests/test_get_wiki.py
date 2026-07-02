"""Tests for wiki content retrieval."""

import asyncio
import concurrent.futures
from pathlib import Path

from thread_wiki.models import ThreadWikiPaths
from thread_wiki.service import run_query

thread_id = "019eec4d-ddf5-7353-bcab-94c41ce68205"
question = "What was BMO's overall financial performance in fiscal 2025, and how did key metrics such as net income, EPS, and ROE change compared to fiscal 2024?"

print("1. Resolving paths")
base_dir = Path("/agent.py").resolve().parent
paths = ThreadWikiPaths.resolve(thread_id, base_dir)

print(f"2. Checking index.md at {paths.wiki_content / 'index.md'}")
index_path = paths.wiki_content / "index.md"
if not index_path.exists():
    print("index.md does not exist")
else:
    print("index.md exists")

topic = f"Thread {thread_id[:8]}"


def _run_coro():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(run_query(paths, topic, question, file_results=False))
    finally:
        loop.close()


print("3. Running thread pool executor")
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
    result = pool.submit(_run_coro).result(timeout=60)
print(f"4. Result: {result.answer if result else 'None'}")
print(f"5. Result length: {len(result.answer) if result else 'None'}")
