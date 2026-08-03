"""Contract tests for thread and run architecture boundaries."""

from __future__ import annotations

import inspect


def test_thread_feature_exposes_repository_and_executor_ports() -> None:
    from webapp.features.threads import RunExecutor, ThreadRepository

    assert ThreadRepository.__module__.endswith("application.ports")
    assert RunExecutor.__module__.endswith("application.ports")
    assert {"get_values", "merge_values"}.issubset(ThreadRepository.__dict__)
    assert "execute" in RunExecutor.__dict__


def test_http_controller_delegates_thread_state_storage() -> None:
    from webapp import routes

    source = inspect.getsource(routes.register_chat_thread_routes)
    assert "_chat_thread_state[" not in source
    assert "_chat_thread_state.get" not in source
    assert "_chat_thread_repository" in source
