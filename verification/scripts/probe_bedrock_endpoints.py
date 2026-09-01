"""Minimal-cost liveness probe for every Bedrock model ID this project has referenced.

Reuses BedrockConverseMessages (the exact adapter production traffic goes through)
with the same tiny, tool-forced request shape as provider.py's own startup
_probe_bedrock check (max_tokens=64) - the cheapest real call per model. Does not
call ListFoundationModels/ListInferenceProfiles: this account's IAM user has no
permission for either (see issues.md, "AWS gives no list API for this account"),
so direct invocation is the only reliable way to check access.

Usage:
    .\\.venv\\Scripts\\python.exe -m verification.scripts.probe_bedrock_endpoints
"""

from backend.app.agent.provider import BedrockConverseMessages

# (model id, what it is / last known status) - extend this list as new ids get
# tried. Keep entries even after they're confirmed dead so re-running this later
# shows whether AWS-side entitlement has changed.
CANDIDATES = [
    ("us.anthropic.claude-sonnet-4-6", "production - investigation role"),
    ("us.anthropic.claude-opus-4-6-v1", "production - synthesis role"),
    ("us.anthropic.claude-opus-4-1-20250805-v1:0", "last known: Marketplace-gated"),
    ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "last known: Marketplace-gated"),
    ("openai.gpt-oss-120b-1:0", "last known: accessible, poor tool-use"),
    ("claude-sonnet-5", "last known: account-ineligible"),
    ("claude-opus-5", "last known: account-ineligible"),
    ("claude-fable-5", "last known: account-ineligible"),
]


def main() -> None:
    messages = BedrockConverseMessages()
    results = []
    for model_id, note in CANDIDATES:
        try:
            response = messages.create(
                _waypoint_model=model_id,
                max_tokens=64,
                system="Return the requested health status.",
                messages=[{"role": "user", "content": "Submit health status ok."}],
                tools=[{
                    "name": "submit_health",
                    "description": "Submit health status.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"status": {"type": "string", "enum": ["ok"]}},
                        "required": ["status"],
                    },
                }],
                tool_choice={"type": "tool", "name": "submit_health"},
            )
            status, detail = "OK", f"stop_reason={getattr(response, 'stop_reason', '?')}"
        except Exception as exc:  # noqa: BLE001 - report every failure, don't filter by type
            status, detail = "FAIL", str(exc)[:220]
        results.append((model_id, note, status, detail))
        print(f"[{status:4}] {model_id:48} ({note})")
        if status == "FAIL":
            print(f"        -> {detail}")

    ok = sum(1 for r in results if r[2] == "OK")
    print(f"\n{ok}/{len(results)} reachable and working right now")


if __name__ == "__main__":
    main()
