from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.app.agent.provider import (
    BedrockConverseMessages,
    BedrockRequestError,
    GeminiMessages,
    GeminiRequestError,
    ModelProviderRouter,
    OpenRouterMessages,
    OpenRouterRequestError,
    ProviderMessages,
    _anthropic_cache_shape,
    _bedrock_client,
    _bedrock_converse_messages,
    _bedrock_supports_prompt_caching,
    _bedrock_tool_config,
    _gemini_contents,
    _gemini_tool_config,
    estimate_model_cost_usd,
    is_billing_or_capacity_error,
)


class BillingError(RuntimeError):
    status_code = 402


class ModelProviderTests(unittest.TestCase):
    def test_openrouter_tool_schema_removes_nonportable_validation_hints(self) -> None:
        schema = OpenRouterMessages._portable_tool_schema({
            "type": "object",
            "properties": {
                "answer": {"type": "string", "minLength": 1, "maxLength": 30},
                "items": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {"type": "integer", "minimum": 1},
                },
            },
            "required": ["answer"],
        })

        self.assertEqual(schema["properties"]["answer"], {"type": "string"})
        self.assertNotIn("maxItems", schema["properties"]["items"])
        self.assertNotIn("minimum", schema["properties"]["items"]["items"])
        self.assertEqual(schema["required"], ["answer"])

    def test_billing_and_capacity_errors_are_recognized(self) -> None:
        self.assertTrue(is_billing_or_capacity_error(BillingError("payment required")))
        self.assertTrue(is_billing_or_capacity_error(RuntimeError("credit balance is low")))
        self.assertTrue(is_billing_or_capacity_error(RuntimeError("rate limit exceeded")))
        self.assertFalse(is_billing_or_capacity_error(ValueError("invalid tool input")))

    def test_bedrock_marketplace_access_denial_is_treated_as_a_fallback_signal(self) -> None:
        # The exact AccessDeniedException Bedrock raises when a specific model needs
        # an AWS Marketplace subscription the account hasn't completed - a per-model
        # entitlement gap, not a credentials problem, so the cascade should advance
        # to the next tier instead of surfacing this as a hard failure.
        message = (
            "An error occurred (AccessDeniedException) when calling the Converse "
            "operation: Model access is denied due to IAM user or service role is "
            "not authorized to perform the required AWS Marketplace actions "
            "(aws-marketplace:ViewSubscriptions, aws-marketplace:Subscribe) to "
            "enable access to this model."
        )
        self.assertTrue(is_billing_or_capacity_error(RuntimeError(message)))

    def test_runtime_billing_failure_switches_to_claude_code(self) -> None:
        router = ModelProviderRouter()
        router.active_provider = "anthropic-api"
        router._initialized = True
        api = SimpleNamespace(
            messages=SimpleNamespace(create=Mock(side_effect=BillingError("no credits")))
        )
        fallback_response = SimpleNamespace(content=[SimpleNamespace(type="tool_use")])
        router._claude_code.messages.create = Mock(return_value=fallback_response)

        def activate(reason: str, exc: BaseException, *, probe: bool) -> None:
            self.assertIn("exhausted", reason)
            self.assertIsInstance(exc, BillingError)
            self.assertFalse(probe)
            router.active_provider = "claude-code"

        with patch.object(router, "_anthropic_client", return_value=api), patch.object(
            router, "activate_claude_code", side_effect=activate
        ), patch(
            "backend.app.agent.provider.settings",
            SimpleNamespace(
                claude_code_fallback=True,
                model_architecture="single",
                bedrock_region=None,
                anthropic_api_key="test-key",
            ),
        ):
            response = ProviderMessages(router).create(messages=[])

        self.assertIs(response, fallback_response)
        router._claude_code.messages.create.assert_called_once_with(messages=[])

    def test_runtime_cascade_tries_bedrock_then_anthropic_then_bedrock_again(self) -> None:
        router = ModelProviderRouter()
        router.active_provider = "bedrock"
        router._initialized = True
        api = SimpleNamespace(
            messages=SimpleNamespace(create=Mock(side_effect=BillingError("no credits")))
        )
        fallback_response = SimpleNamespace(content=[SimpleNamespace(type="tool_use")])
        bedrock_create = Mock(
            side_effect=[
                BedrockRequestError("ThrottlingException: too many tokens per day"),
                BedrockRequestError("ThrottlingException: too many tokens per day"),
            ]
        )
        router._bedrock.messages.create = bedrock_create
        router._claude_code.messages.create = Mock(return_value=fallback_response)

        with patch.object(router, "_anthropic_client", return_value=api), patch(
            "backend.app.agent.provider.settings",
            SimpleNamespace(
                claude_code_fallback=True,
                model_architecture="single",
                bedrock_region="us-east-1",
                bedrock_primary_model="us.anthropic.claude-sonnet-4-6",
                anthropic_api_key="test-key",
            ),
        ):
            response = ProviderMessages(router).create(messages=[])

        self.assertIs(response, fallback_response)
        self.assertEqual(bedrock_create.call_count, 2)
        for call in bedrock_create.call_args_list:
            self.assertEqual(call.kwargs["_waypoint_model"], "us.anthropic.claude-sonnet-4-6")
        api.messages.create.assert_called_once_with(messages=[])
        router._claude_code.messages.create.assert_called_once_with(messages=[])

    def test_runtime_cascade_stops_at_first_successful_tier(self) -> None:
        router = ModelProviderRouter()
        router.active_provider = "bedrock"
        router._initialized = True
        bedrock_response = SimpleNamespace(content=[SimpleNamespace(type="tool_use")])
        bedrock_create = Mock(return_value=bedrock_response)
        router._bedrock.messages.create = bedrock_create
        router._claude_code.messages.create = Mock(
            side_effect=AssertionError("must not reach Claude Code when Bedrock succeeds")
        )

        with patch(
            "backend.app.agent.provider.settings",
            SimpleNamespace(
                claude_code_fallback=True,
                model_architecture="single",
                bedrock_region="us-east-1",
                bedrock_primary_model="us.anthropic.claude-sonnet-4-6",
                anthropic_api_key="test-key",
            ),
        ):
            response = ProviderMessages(router).create(messages=[])

        self.assertIs(response, bedrock_response)
        bedrock_create.assert_called_once()
        router._claude_code.messages.create.assert_not_called()
        self.assertEqual(router.active_provider, "bedrock")

    def test_non_capacity_model_error_is_not_hidden_by_fallback(self) -> None:
        router = ModelProviderRouter()
        router.active_provider = "anthropic-api"
        api = SimpleNamespace(
            messages=SimpleNamespace(create=Mock(side_effect=ValueError("bad request")))
        )
        with patch.object(router, "_anthropic_client", return_value=api), patch(
            "backend.app.agent.provider.settings",
            SimpleNamespace(
                claude_code_fallback=True,
                model_architecture="single",
                bedrock_region=None,
                anthropic_api_key="test-key",
            ),
        ):
            with self.assertRaisesRegex(ValueError, "bad request"):
                ProviderMessages(router).create(messages=[])

    def test_openrouter_adapter_translates_tools_and_response(self) -> None:
        configured = SimpleNamespace(
            openrouter_api_key="test-key",
            openrouter_base_url="https://openrouter.example/api/v1",
            openrouter_timeout_seconds=30,
            agent_max_output_tokens=2000,
        )
        http_response = Mock(status_code=200, text="")
        http_response.json.return_value = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "call-1",
                        "function": {
                            "name": "find_symbols",
                            "arguments": '{"query":"router"}',
                        },
                    }],
                },
            }],
            "usage": {"prompt_tokens": 120, "completion_tokens": 14, "cost": 0},
        }
        with patch("backend.app.agent.provider.settings", configured), patch(
            "backend.app.agent.provider.httpx.post", return_value=http_response
        ) as post:
            response = OpenRouterMessages().create(
                _waypoint_model="cohere/north-mini-code:free",
                _waypoint_role="investigation",
                max_tokens=500,
                system="Investigate with tools.",
                messages=[{"role": "user", "content": "Find the router."}],
                tools=[{
                    "name": "find_symbols",
                    "description": "Find symbols.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }],
                tool_choice={"type": "auto"},
            )

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "cohere/north-mini-code:free")
        self.assertEqual(payload["tools"][0]["function"]["name"], "find_symbols")
        self.assertEqual(response.content[0].name, "find_symbols")
        self.assertEqual(response.content[0].input, {"query": "router"})
        self.assertEqual(response.waypoint_role, "investigation")
        self.assertEqual(response.usage.input_tokens, 120)

    def test_openrouter_retries_rate_limit_then_succeeds(self) -> None:
        configured = SimpleNamespace(
            openrouter_api_key="test-key",
            openrouter_base_url="https://openrouter.example/api/v1",
            openrouter_timeout_seconds=30,
            agent_max_output_tokens=2000,
        )
        limited = Mock(status_code=429, text="", headers={"Retry-After": "0.25"})
        limited.json.return_value = {"error": {"code": 429, "message": "limited"}}
        success = Mock(status_code=200, text="", headers={})
        success.json.return_value = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "done", "tool_calls": []},
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }
        with patch("backend.app.agent.provider.settings", configured), patch(
            "backend.app.agent.provider.httpx.post",
            side_effect=[limited, success],
        ) as post, patch("backend.app.agent.provider.time.sleep") as sleep:
            response = OpenRouterMessages().create(
                _waypoint_model="poolside/test:free",
                messages=[{"role": "user", "content": "hello"}],
            )

        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(0.25)
        self.assertEqual(response.content[0].text, "done")

    def test_openrouter_does_not_retry_exhausted_daily_free_quota(self) -> None:
        configured = SimpleNamespace(
            openrouter_api_key="test-key",
            openrouter_base_url="https://openrouter.example/api/v1",
            openrouter_timeout_seconds=30,
            agent_max_output_tokens=2000,
        )
        limited = Mock(status_code=429, text="", headers={})
        limited.json.return_value = {
            "error": {
                "code": 429,
                "message": "Rate limit exceeded: free-models-per-day. Add 10 credits.",
            }
        }
        with patch("backend.app.agent.provider.settings", configured), patch(
            "backend.app.agent.provider.httpx.post", return_value=limited
        ) as post, patch("backend.app.agent.provider.time.sleep") as sleep:
            with self.assertRaisesRegex(OpenRouterRequestError, "free-models-per-day"):
                OpenRouterMessages().create(
                    _waypoint_model="poolside/test:free",
                    messages=[{"role": "user", "content": "hello"}],
                )

        post.assert_called_once()
        sleep.assert_not_called()

    def test_dual_cascade_stays_on_claude_code_once_sticky(self) -> None:
        # Once a prior request in this process exhausted every other tier and
        # activated the Claude Code fallback, active_provider is sticky - the
        # shared dual-role cascade must not re-attempt Bedrock/API for later
        # requests, regardless of role. This replaces the old
        # investigation_provider/synthesis_provider-based test: those settings
        # are no longer read for live routing (one shared cascade now decides
        # the provider for both roles), only by comparison.py's separate
        # create_for_endpoint benchmarking path.
        configured = self._dual_settings()
        router = ModelProviderRouter()
        router.active_provider = "claude-code"
        expected = SimpleNamespace(content=[])
        router._claude_code.messages.create = Mock(return_value=expected)
        router._bedrock.messages.create = Mock()  # must not be called
        with patch("backend.app.agent.provider.settings", configured):
            response = ProviderMessages(router).create(
                messages=[],
                tool_choice={"type": "tool", "name": "submit_answer"},
            )

        self.assertIs(response, expected)
        routed = router._claude_code.messages.create.call_args.kwargs
        self.assertEqual(routed["_waypoint_role"], "synthesis")
        # Claude Code has one shared model regardless of role (config.py has a
        # single claude_code_model field, not per-role variants).
        self.assertEqual(routed["_waypoint_model"], "claude-sonnet-5")
        router._bedrock.messages.create.assert_not_called()

    def _dual_settings(self, **overrides: object) -> SimpleNamespace:
        """Build a fake `settings` for the dual-role shared-cascade tests below,
        with sensible defaults for every field `_create_dual_cascade` and
        `_role_model_for_provider` read; **overrides replaces individual fields
        per test."""
        base = dict(
            model_architecture="dual",
            investigation_model="claude-sonnet-5",
            investigation_bedrock_model="us.anthropic.claude-sonnet-4-6",
            synthesis_model="claude-opus-5",
            synthesis_bedrock_model="us.anthropic.claude-opus-4-6-v1",
            bedrock_region="us-east-1",
            anthropic_api_key="sk-ant-test",
            claude_code_fallback=True,
            claude_code_model="claude-sonnet-5",
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_dual_role_prefers_bedrock_with_the_roles_own_model(self) -> None:
        configured = self._dual_settings()
        router = ModelProviderRouter()
        router._bedrock.messages.create = Mock(return_value=SimpleNamespace(content=[]))
        router._anthropic_client = Mock()  # must not be called
        with patch("backend.app.agent.provider.settings", configured):
            router.create_for_role("synthesis", {"messages": []})

        routed = router._bedrock.messages.create.call_args.kwargs
        self.assertEqual(routed["_waypoint_model"], "us.anthropic.claude-opus-4-6-v1")
        self.assertEqual(routed["_waypoint_role"], "synthesis")
        router._anthropic_client.assert_not_called()

    def test_dual_role_falls_back_to_anthropic_api_on_bedrock_billing_error(self) -> None:
        configured = self._dual_settings()
        router = ModelProviderRouter()
        router._bedrock.messages.create = Mock(side_effect=BillingError("throttling"))
        api_response = SimpleNamespace(content=[])
        anthropic_client = SimpleNamespace(
            messages=SimpleNamespace(create=Mock(return_value=api_response))
        )
        router._anthropic_client = Mock(return_value=anthropic_client)
        with patch("backend.app.agent.provider.settings", configured):
            response = router.create_for_role("investigation", {"messages": []})

        self.assertIs(response, api_response)
        routed = anthropic_client.messages.create.call_args.kwargs
        self.assertEqual(routed["model"], "claude-sonnet-5")
        # Bedrock was tried before falling through, using the investigation role's
        # own model - not the synthesis one.
        first_attempt = router._bedrock.messages.create.call_args.kwargs
        self.assertEqual(first_attempt["_waypoint_model"], "us.anthropic.claude-sonnet-4-6")

    def test_dual_role_falls_back_to_claude_code_once_bedrock_and_api_are_exhausted(
        self,
    ) -> None:
        configured = self._dual_settings()
        router = ModelProviderRouter()
        router._bedrock.messages.create = Mock(side_effect=BillingError("throttling"))
        anthropic_client = SimpleNamespace(
            messages=SimpleNamespace(create=Mock(side_effect=BillingError("no credits")))
        )
        router._anthropic_client = Mock(return_value=anthropic_client)
        expected = SimpleNamespace(content=[])
        router._claude_code.messages.create = Mock(return_value=expected)
        with (
            patch("backend.app.agent.provider.settings", configured),
            patch.object(router, "_probe_claude_code"),
        ):
            response = router.create_for_role("synthesis", {"messages": []})

        self.assertIs(response, expected)
        self.assertEqual(router.active_provider, "claude-code")
        routed = router._claude_code.messages.create.call_args.kwargs
        self.assertEqual(routed["_waypoint_role"], "synthesis")
        # Claude Code has one shared model regardless of role - unlike Bedrock and
        # the Anthropic API, config.py has no per-role claude_code_model setting.
        self.assertEqual(routed["_waypoint_model"], "claude-sonnet-5")
        # Bedrock was retried a second time (2 attempts) before giving up to Claude
        # Code, matching the single-model cascade's shape.
        self.assertEqual(router._bedrock.messages.create.call_count, 2)

    def test_dual_role_does_not_clobber_the_dual_summary_on_ordinary_success(
        self,
    ) -> None:
        configured = self._dual_settings()
        router = ModelProviderRouter()
        router.active_provider = "dual"
        router._bedrock.messages.create = Mock(return_value=SimpleNamespace(content=[]))
        with patch("backend.app.agent.provider.settings", configured):
            router.create_for_role("investigation", {"messages": []})

        self.assertEqual(router.active_provider, "dual")

    def test_dual_cascade_prefers_the_conversation_scoped_provider_hint(self) -> None:
        # backend/app/agent/service.py's answer() loop (and the equivalent loops in
        # onboarding.py/issues.py) pass the previous round's resolved provider back
        # in as _waypoint_conversation_provider so a later round of the SAME
        # conversation stays on that tier instead of independently re-resolving -
        # this is what makes cross-provider mixing within one conversation
        # impossible by construction now that investigation/synthesis share one
        # cascade (previously, mixing Bedrock's SimpleNamespace-shaped history
        # into a later round served by the real Anthropic SDK would crash on
        # json.dumps).
        configured = self._dual_settings()
        router = ModelProviderRouter()
        router._bedrock.messages.create = Mock()  # must not be called
        api_response = SimpleNamespace(content=[])
        anthropic_client = SimpleNamespace(
            messages=SimpleNamespace(create=Mock(return_value=api_response))
        )
        router._anthropic_client = Mock(return_value=anthropic_client)
        with patch("backend.app.agent.provider.settings", configured):
            response = router.create_for_role(
                "synthesis",
                {"messages": [], "_waypoint_conversation_provider": "anthropic-api"},
            )

        self.assertIs(response, api_response)
        routed = anthropic_client.messages.create.call_args.kwargs
        self.assertEqual(routed["model"], "claude-opus-5")
        router._bedrock.messages.create.assert_not_called()

    def test_dual_cascade_falls_through_when_the_preferred_provider_itself_fails(
        self,
    ) -> None:
        configured = self._dual_settings()
        router = ModelProviderRouter()
        router._bedrock.messages.create = Mock(side_effect=BillingError("throttling"))
        api_response = SimpleNamespace(content=[])
        anthropic_client = SimpleNamespace(
            messages=SimpleNamespace(create=Mock(return_value=api_response))
        )
        router._anthropic_client = Mock(return_value=anthropic_client)
        with patch("backend.app.agent.provider.settings", configured):
            response = router.create_for_role(
                "investigation",
                {"messages": [], "_waypoint_conversation_provider": "bedrock"},
            )

        self.assertIs(response, api_response)
        # Bedrock (both the preferred provider and the cascade's normal first
        # tier here) was still attempted, then the cascade fell through to the
        # Anthropic API on its billing/capacity failure rather than giving up.
        router._bedrock.messages.create.assert_called_once()

    def test_auto_provider_is_available_from_bedrock_region_alone(self) -> None:
        # "auto" is still a real _provider_available branch, used by
        # create_for_endpoint for comparison.py's benchmarking feature (whose
        # investigation_provider/synthesis_provider settings default to "auto") -
        # unrelated to the dual-role live cascade, which no longer reads a
        # per-role provider setting at all.
        configured = self._dual_settings(
            anthropic_api_key=None, claude_code_fallback=False
        )
        router = ModelProviderRouter()
        with patch("backend.app.agent.provider.settings", configured):
            self.assertTrue(router._provider_available("auto"))

    def test_bedrock_converse_messages_translates_tool_blocks(self) -> None:
        assistant_tool_use = SimpleNamespace(
            type="tool_use", id="call-1", name="find_symbols", input={"query": "router"}
        )
        assistant_text = SimpleNamespace(type="text", text="Looking it up.")
        messages = [
            {"role": "user", "content": "Find the router."},
            {"role": "assistant", "content": [assistant_text, assistant_tool_use]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call-1", "content": "found it"},
                ],
            },
        ]

        converted = _bedrock_converse_messages(messages)

        self.assertEqual(converted[0], {"role": "user", "content": [{"text": "Find the router."}]})
        self.assertEqual(converted[1]["content"][0], {"text": "Looking it up."})
        self.assertEqual(
            converted[1]["content"][1],
            {"toolUse": {"toolUseId": "call-1", "name": "find_symbols", "input": {"query": "router"}}},
        )
        self.assertEqual(
            converted[2]["content"][0],
            {"toolResult": {
                "toolUseId": "call-1", "content": [{"text": "found it"}], "status": "success",
            }},
        )

    def test_bedrock_tool_config_translates_schema_and_forced_choice(self) -> None:
        config = _bedrock_tool_config(
            [{
                "name": "find_symbols",
                "description": "Find symbols.",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            }],
            {"type": "tool", "name": "find_symbols"},
        )

        self.assertEqual(config["tools"][0]["toolSpec"]["name"], "find_symbols")
        self.assertEqual(
            config["tools"][0]["toolSpec"]["inputSchema"]["json"]["properties"]["query"],
            {"type": "string"},
        )
        self.assertEqual(config["toolChoice"], {"tool": {"name": "find_symbols"}})

    def test_bedrock_adapter_translates_request_and_response(self) -> None:
        configured = SimpleNamespace(
            bedrock_region="us-east-1",
            bedrock_access_key_id=None,
            bedrock_secret_access_key=None,
            agent_max_output_tokens=2000,
        )
        bedrock_response = {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"toolUse": {
                            "toolUseId": "call-9", "name": "find_symbols",
                            "input": {"query": "router"},
                        }},
                    ],
                }
            },
            "stopReason": "tool_use",
            "usage": {
                "inputTokens": 120,
                "outputTokens": 14,
                "cacheReadInputTokens": 80,
                "cacheWriteInputTokens": 0,
            },
        }
        mock_client = Mock()
        mock_client.converse.return_value = bedrock_response
        with patch("backend.app.agent.provider.settings", configured), patch(
            "backend.app.agent.provider._bedrock_client", return_value=mock_client
        ):
            response = BedrockConverseMessages().create(
                _waypoint_model="anthropic.claude-sonnet-4-6",
                _waypoint_role="investigation",
                system="Investigate with tools.",
                messages=[{"role": "user", "content": "Find the router."}],
                tools=[{
                    "name": "find_symbols",
                    "description": "Find symbols.",
                    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
                }],
                tool_choice={"type": "auto"},
                max_tokens=500,
            )

        call_kwargs = mock_client.converse.call_args.kwargs
        self.assertEqual(call_kwargs["modelId"], "anthropic.claude-sonnet-4-6")
        # Claude models get a cachePoint on the system prompt and on the tail of the
        # messages list, so a later round of the same conversation (whose messages
        # list is a superset of this one) can hit a cache read instead of being
        # rebilled at full input price.
        self.assertEqual(
            call_kwargs["system"],
            [{"text": "Investigate with tools."}, {"cachePoint": {"type": "default"}}],
        )
        self.assertEqual(
            call_kwargs["messages"][-1]["content"][-1],
            {"cachePoint": {"type": "default"}},
        )
        self.assertEqual(call_kwargs["inferenceConfig"]["maxTokens"], 500)
        self.assertEqual(call_kwargs["toolConfig"]["tools"][0]["toolSpec"]["name"], "find_symbols")
        self.assertEqual(response.content[0].type, "tool_use")
        self.assertEqual(response.content[0].name, "find_symbols")
        self.assertEqual(response.content[0].input, {"query": "router"})
        self.assertEqual(response.waypoint_provider, "bedrock:anthropic.claude-sonnet-4-6")
        self.assertEqual(response.waypoint_role, "investigation")
        self.assertEqual(response.usage.input_tokens, 120)
        self.assertEqual(response.usage.output_tokens, 14)
        self.assertEqual(response.usage.cache_read_input_tokens, 80)
        self.assertEqual(response.usage.cache_creation_input_tokens, 0)

    def test_bedrock_adapter_skips_cache_points_for_non_claude_models(self) -> None:
        # BedrockConverseMessages also drives non-Anthropic models (comparison
        # runs) - cachePoint support isn't confirmed for every model family
        # Converse hosts, so it must only be injected for Claude models.
        configured = SimpleNamespace(
            bedrock_region="us-east-1",
            bedrock_access_key_id=None,
            bedrock_secret_access_key=None,
            agent_max_output_tokens=2000,
        )
        bedrock_response = {
            "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 2},
        }
        mock_client = Mock()
        mock_client.converse.return_value = bedrock_response
        with patch("backend.app.agent.provider.settings", configured), patch(
            "backend.app.agent.provider._bedrock_client", return_value=mock_client
        ):
            BedrockConverseMessages().create(
                _waypoint_model="openai.gpt-oss-120b-1:0",
                _waypoint_role="investigation",
                system="Investigate with tools.",
                messages=[{"role": "user", "content": "Find the router."}],
                max_tokens=500,
            )

        call_kwargs = mock_client.converse.call_args.kwargs
        self.assertEqual(call_kwargs["system"], [{"text": "Investigate with tools."}])
        self.assertNotIn(
            {"cachePoint": {"type": "default"}}, call_kwargs["messages"][-1]["content"]
        )

    def test_bedrock_supports_prompt_caching_matches_claude_ids_only(self) -> None:
        self.assertTrue(_bedrock_supports_prompt_caching("us.anthropic.claude-sonnet-4-6"))
        self.assertTrue(_bedrock_supports_prompt_caching("anthropic.claude-opus-4-6-v1"))
        self.assertFalse(_bedrock_supports_prompt_caching("openai.gpt-oss-120b-1:0"))
        self.assertFalse(_bedrock_supports_prompt_caching("meta.llama3-70b-instruct-v1:0"))

    def test_anthropic_cache_shape_wraps_string_system_and_last_message(self) -> None:
        system, messages = _anthropic_cache_shape(
            "You are an issue investigator.",
            [
                {"role": "user", "content": "GITHUB ISSUE #1"},
                {"role": "assistant", "content": "looking"},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "call-1", "content": "found it"},
                ]},
            ],
        )

        self.assertEqual(
            system,
            [{
                "type": "text",
                "text": "You are an issue investigator.",
                "cache_control": {"type": "ephemeral"},
            }],
        )
        # Only the LAST message is marked - earlier turns are untouched.
        self.assertEqual(messages[0]["content"], "GITHUB ISSUE #1")
        self.assertEqual(messages[1]["content"], "looking")
        self.assertEqual(
            messages[2]["content"][-1],
            {
                "type": "tool_result",
                "tool_use_id": "call-1",
                "content": "found it",
                "cache_control": {"type": "ephemeral"},
            },
        )

    def test_anthropic_cache_shape_does_not_mutate_the_callers_messages_list(self) -> None:
        original_messages = [{"role": "user", "content": "hello"}]
        _anthropic_cache_shape("system prompt", original_messages)

        # The caller's own list/dict must be untouched - issues.py/service.py keep
        # appending to their own `messages` variable across rounds, so mutating
        # shared objects here would leak cache_control markers into later rounds.
        self.assertEqual(original_messages, [{"role": "user", "content": "hello"}])

    def test_anthropic_cache_shape_is_a_no_op_for_empty_system_and_messages(self) -> None:
        system, messages = _anthropic_cache_shape("", [])
        self.assertEqual(system, "")
        self.assertEqual(messages, [])

    def test_estimate_model_cost_usd_prices_bedrock_and_first_party_ids_alike(self) -> None:
        cost = estimate_model_cost_usd(
            "us.anthropic.claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=1_000_000
        )
        self.assertEqual(cost, 18.0)  # $3 input + $15 output per 1M tokens

        cost = estimate_model_cost_usd(
            "us.anthropic.claude-opus-4-6-v1", input_tokens=1_000_000, output_tokens=1_000_000
        )
        self.assertEqual(cost, 30.0)  # $5 input + $25 output per 1M tokens

    def test_estimate_model_cost_usd_discounts_cache_reads_and_surcharges_writes(self) -> None:
        cost = estimate_model_cost_usd(
            "us.anthropic.claude-sonnet-4-6",
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=1_000_000,
            cache_creation_input_tokens=1_000_000,
        )
        # 1M cache-read tokens at 0.1x the $3 input rate, plus 1M cache-write
        # tokens at 1.25x the $3 input rate.
        self.assertAlmostEqual(cost, 0.3 + 3.75)

    def test_estimate_model_cost_usd_returns_none_for_missing_tokens_or_unknown_model(
        self,
    ) -> None:
        self.assertIsNone(
            estimate_model_cost_usd("us.anthropic.claude-sonnet-4-6", None, 100)
        )
        self.assertIsNone(
            estimate_model_cost_usd("some-unlisted-model", input_tokens=10, output_tokens=10)
        )

    def test_bedrock_client_requires_region(self) -> None:
        configured = SimpleNamespace(bedrock_region=None)
        with patch("backend.app.agent.provider.settings", configured):
            with self.assertRaisesRegex(BedrockRequestError, "region is not configured"):
                _bedrock_client()

    def test_bedrock_request_wraps_converse_failures(self) -> None:
        configured = SimpleNamespace(
            bedrock_region="us-east-1",
            bedrock_access_key_id=None,
            bedrock_secret_access_key=None,
            agent_max_output_tokens=2000,
        )
        mock_client = Mock()
        mock_client.converse.side_effect = RuntimeError("model not found")
        with patch("backend.app.agent.provider.settings", configured), patch(
            "backend.app.agent.provider._bedrock_client", return_value=mock_client
        ):
            with self.assertRaisesRegex(BedrockRequestError, "model not found"):
                BedrockConverseMessages().create(
                    _waypoint_model="openai.gpt-oss-120b-1:0",
                    messages=[{"role": "user", "content": "hi"}],
                )

    def test_create_for_endpoint_routes_to_bedrock(self) -> None:
        router = ModelProviderRouter()
        expected = SimpleNamespace(content=[])
        router._bedrock.messages.create = Mock(return_value=expected)
        with patch(
            "backend.app.agent.provider.settings",
            SimpleNamespace(bedrock_region="us-east-1"),
        ):
            response = router.create_for_endpoint(
                "bedrock", "anthropic.claude-sonnet-4-6", "investigation", {"messages": []}
            )

        self.assertIs(response, expected)
        routed = router._bedrock.messages.create.call_args.kwargs
        self.assertEqual(routed["_waypoint_model"], "anthropic.claude-sonnet-4-6")

    def test_gemini_contents_translates_tool_blocks_and_matches_by_name(self) -> None:
        assistant_tool_use = SimpleNamespace(
            type="tool_use", id="call-1", name="find_symbols", input={"query": "router"}
        )
        assistant_text = SimpleNamespace(type="text", text="Looking it up.")
        messages = [
            {"role": "user", "content": "Find the router."},
            {"role": "assistant", "content": [assistant_text, assistant_tool_use]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call-1", "content": "found it"},
                ],
            },
        ]

        converted = _gemini_contents(messages)

        self.assertEqual(converted[0], {"role": "user", "parts": [{"text": "Find the router."}]})
        self.assertEqual(converted[1]["role"], "model")
        self.assertEqual(converted[1]["parts"][0], {"text": "Looking it up."})
        self.assertEqual(
            converted[1]["parts"][1],
            {"functionCall": {"name": "find_symbols", "args": {"query": "router"}}},
        )
        self.assertEqual(converted[2]["role"], "user")
        self.assertEqual(
            converted[2]["parts"][0],
            {"functionResponse": {"name": "find_symbols", "response": {"content": "found it"}}},
        )

    def test_gemini_tool_config_translates_schema_and_forced_choice(self) -> None:
        declarations, tool_config = _gemini_tool_config(
            [{
                "name": "find_symbols",
                "description": "Find symbols.",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            }],
            {"type": "tool", "name": "find_symbols"},
        )

        self.assertEqual(declarations[0]["name"], "find_symbols")
        self.assertEqual(declarations[0]["parameters"]["properties"]["query"], {"type": "string"})
        self.assertEqual(
            tool_config["functionCallingConfig"],
            {"mode": "ANY", "allowedFunctionNames": ["find_symbols"]},
        )

    def test_gemini_adapter_translates_request_and_response(self) -> None:
        configured = SimpleNamespace(
            gemini_api_key="test-key",
            gemini_base_url="https://gemini.example/v1beta",
            gemini_timeout_seconds=30,
            gemini_fallback_model="gemini-3.1-pro-preview",
            agent_max_output_tokens=2000,
        )
        gemini_response = Mock(status_code=200, text="")
        gemini_response.json.return_value = {
            "candidates": [{
                "content": {
                    "role": "model",
                    "parts": [
                        {"functionCall": {"name": "find_symbols", "args": {"query": "router"}}},
                    ],
                },
                "finishReason": "STOP",
            }],
            "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 14},
        }
        with patch("backend.app.agent.provider.settings", configured), patch(
            "backend.app.agent.provider.httpx.post", return_value=gemini_response
        ) as post:
            response = GeminiMessages().create(
                _waypoint_role="investigation",
                system="Investigate with tools.",
                messages=[{"role": "user", "content": "Find the router."}],
                tools=[{
                    "name": "find_symbols",
                    "description": "Find symbols.",
                    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
                }],
                tool_choice={"type": "auto"},
                max_tokens=500,
            )

        self.assertEqual(
            post.call_args.args[0],
            "https://gemini.example/v1beta/models/gemini-3.1-pro-preview:generateContent",
        )
        self.assertEqual(post.call_args.kwargs["params"], {"key": "test-key"})
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["systemInstruction"], {"parts": [{"text": "Investigate with tools."}]})
        self.assertEqual(payload["tools"][0]["functionDeclarations"][0]["name"], "find_symbols")
        self.assertEqual(response.content[0].type, "tool_use")
        self.assertEqual(response.content[0].name, "find_symbols")
        self.assertEqual(response.content[0].input, {"query": "router"})
        self.assertEqual(response.stop_reason, "tool_use")
        self.assertEqual(response.waypoint_provider, "gemini:gemini-3.1-pro-preview")
        self.assertEqual(response.waypoint_role, "investigation")
        self.assertEqual(response.usage.input_tokens, 120)
        self.assertEqual(response.usage.output_tokens, 14)

    def test_gemini_request_wraps_http_error_response(self) -> None:
        configured = SimpleNamespace(
            gemini_api_key="test-key",
            gemini_base_url="https://gemini.example/v1beta",
            gemini_timeout_seconds=30,
            gemini_fallback_model="gemini-3.1-pro-preview",
            agent_max_output_tokens=2000,
        )
        error_response = Mock(status_code=429, text="")
        error_response.json.return_value = {"error": {"code": 429, "message": "rate limited"}}
        with patch("backend.app.agent.provider.settings", configured), patch(
            "backend.app.agent.provider.httpx.post", return_value=error_response
        ):
            with self.assertRaisesRegex(GeminiRequestError, "rate limited"):
                GeminiMessages().create(messages=[{"role": "user", "content": "hi"}])

    def test_create_for_endpoint_routes_to_gemini(self) -> None:
        router = ModelProviderRouter()
        expected = SimpleNamespace(content=[])
        router._gemini.messages.create = Mock(return_value=expected)
        with patch(
            "backend.app.agent.provider.settings",
            SimpleNamespace(gemini_api_key="test-key"),
        ):
            response = router.create_for_endpoint(
                "gemini", "gemini-3.1-pro-preview", "investigation", {"messages": []}
            )

        self.assertIs(response, expected)
        routed = router._gemini.messages.create.call_args.kwargs
        self.assertEqual(routed["_waypoint_model"], "gemini-3.1-pro-preview")


if __name__ == "__main__":
    unittest.main()
