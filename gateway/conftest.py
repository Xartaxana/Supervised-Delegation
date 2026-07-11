"""Shared pytest fixtures for the gateway test suite.

Test isolation (D-0016, Delegated Task 4; born from the 2026-07-07
Lead review finding #1): with an unlucky collection order, a test
that leaves litellm.callbacks pointed at the real SQLiteLogger can
have its callback fire on a LATER test's mock litellm.completion
calls, after that later test's own GATEWAY_DB_PATH monkeypatch has
already been torn down -- writing rows into the real
gateway/requests.db. This happened for real on 2026-07-04 (16 mock
rows in two pytest clusters at 19:08 and 19:10; see CURRENT_CONTEXT.md
"Delegated Task 4"). Alphabetical collection order
(test_shadow_eval.py before test_sqlite_logger.py) happened to hide it.

This autouse, function-scoped fixture removes both preconditions for
every test in the suite, not just the ones that remembered to guard
themselves:

1. GATEWAY_DB_PATH always points at a per-test tmp_path database
   before the test body runs. A test that calls
   monkeypatch.setenv("GATEWAY_DB_PATH", ...) itself still wins: pytest
   fixture teardown is LIFO, so the test's own monkeypatch (requested
   after this fixture) is undone first, correctly re-exposing this
   fixture's tmp path underneath it -- there is never a window where
   GATEWAY_DB_PATH is unset or pointed at the real database.
2. ALL litellm callback lists are saved before the test and restored
   after, regardless of whether the test remembers to clean up.
   Restoring litellm.callbacks alone is NOT enough: verified
   empirically on litellm 1.90.2 (2026-07-07) that one completion
   call with litellm.callbacks=[logger] copies the logger into
   success_callback, failure_callback, input_callback,
   _async_success_callback and _async_failure_callback (litellm's
   function_setup does the copying at call time), and the leaked
   success_callback entry then fires on later tests' mock completion
   calls from a worker thread -- after this fixture's env teardown,
   i.e. straight into the real gateway/requests.db.
"""

import litellm
import pytest

_LITELLM_CALLBACK_LISTS = (
    "callbacks",
    "success_callback",
    "failure_callback",
    "input_callback",
    "_async_success_callback",
    "_async_failure_callback",
)


@pytest.fixture(autouse=True)
def _isolate_gateway_state(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_PATH", str(tmp_path / "requests.db"))

    saved = {name: list(getattr(litellm, name)) for name in _LITELLM_CALLBACK_LISTS}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(litellm, name, value)
