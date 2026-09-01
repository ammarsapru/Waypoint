from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.app.observability import (
    log_event,
    register_event_hook,
    register_function_hook,
    sanitize,
    trace_context,
    traced,
)


class SanitizeTokenCountTests(unittest.TestCase):
    """`_SENSITIVE_KEY`'s "token" branch exists for credential fields
    (`github_token`, `access_token`, ...), but "token" is also a substring of every
    token-*count* field this codebase logs (`input_tokens`, `max_tokens`,
    `token_count`, ...). Regression coverage for the 2026-07-30 fix that stopped
    blanket-redacting the latter - see the comment above `_TOKEN_COUNT_KEY` in
    observability.py for how the historical data loss was discovered."""

    def test_token_count_fields_are_not_redacted(self) -> None:
        for key in (
            "input_tokens", "output_tokens", "total_tokens", "max_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens",
            "requested_max_output_tokens", "token_count", "tokens_per_tool_call",
            "output_tokens_per_second",
        ):
            self.assertEqual(sanitize(1234, key=key), 1234, key)

    def test_credential_token_fields_are_still_redacted(self) -> None:
        for key in (
            "github_token", "access_token", "auth_token", "session_token",
            "api_key", "authorization",
        ):
            self.assertEqual(sanitize("sk-should-not-leak", key=key), "<redacted>", key)


class FunctionHookTests(unittest.TestCase):
    def test_structured_event_hook_receives_correlated_sanitized_fields(self) -> None:
        events: list[dict] = []
        unregister = register_event_hook(lambda payload: events.append(dict(payload)))
        try:
            with trace_context("event-trace", "event-span"):
                log_event(
                    __import__("logging").getLogger(__name__),
                    __import__("logging").INFO,
                    "test.provider_called",
                    "Provider called",
                    api_key="must-not-leak",
                    model="poolside/test",
                )
        finally:
            unregister()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["trace_id"], "event-trace")
        self.assertEqual(events[0]["span_id"], "event-span")
        self.assertEqual(events[0]["fields"]["api_key"], "<redacted>")
        self.assertEqual(events[0]["fields"]["model"], "poolside/test")

    def test_traced_function_emits_pre_post_and_error_hooks(self) -> None:
        events: list[tuple[str, dict]] = []
        unregister = register_function_hook(
            lambda phase, payload: events.append((phase, dict(payload)))
        )

        @traced("test.hooked")
        def hooked(value: int, fail: bool = False) -> dict[str, int]:
            if fail:
                raise ValueError("expected failure")
            return {"produced": value * 2}

        fake_settings = SimpleNamespace(trace_functions=True, log_value_limit=1200)
        try:
            with patch("backend.app.observability.settings", fake_settings):
                with trace_context("benchmark-case-model"):
                    self.assertEqual(hooked(3), {"produced": 6})
                    with self.assertRaises(ValueError):
                        hooked(4, fail=True)
        finally:
            unregister()

        self.assertEqual([phase for phase, _ in events], ["pre", "post", "pre", "error"])
        self.assertEqual(events[0][1]["arguments"]["value"], 3)
        self.assertTrue(all(payload["trace_id"] == "benchmark-case-model" for _, payload in events))
        self.assertEqual(events[1][1]["result"], {"produced": 6})
        self.assertEqual(events[-1][1]["exception_type"], "ValueError")


if __name__ == "__main__":
    unittest.main()
