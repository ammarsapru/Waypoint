# Claude Agent SDK TTFT Measurement Report

Generated from the live Waypoint comparison run on July 26, 2026.

## Direct answer

Waypoint can measure the time until the Claude Agent SDK exposes its **first assistant
event**, and it does. It cannot currently measure true token TTFT through that SDK because
the SDK iteration used by Waypoint yields an `AssistantMessage` and a final
`ResultMessage`, not the underlying text or tool-call token deltas.

Those are related but different measurements:

| Metric | Start | Stop | Available for Claude Agent SDK? |
|---|---|---|---|
| True token TTFT | Immediately before request dispatch | First streamed text/tool token delta | No |
| First observable SDK event | Immediately before `query(...)` iteration | First `AssistantMessage` yielded | Yes |
| End-to-end latency | Immediately before request dispatch | Validated structured result available | Yes |

Calling the first assistant event “TTFT” would overstate what the SDK exposes. Waypoint
therefore records it as `first_observable_event_ms`, leaves `ttft_ms` null, and includes a
specific status explaining the limitation.

## Where it is measured

In `backend/app/agent/provider.py`, `ClaudeCodeMessages._create`:

```python
started = time.perf_counter()
async for message in query(prompt=prompt, options=options):
    if isinstance(message, AssistantMessage) and first_assistant_event_ms is None:
        first_assistant_event_ms = round(
            (time.perf_counter() - started) * 1000, 3
        )
```

The adapter now returns this independently from token TTFT:

```python
waypoint_ttft_ms=None,
waypoint_first_observable_event_ms=first_assistant_event_ms,
waypoint_ttft_status=(
    "unavailable_sdk_first_assistant_event_ms="
    f"{first_assistant_event_ms}"
),
```

`ModelComparisonAnswer.first_observable_event_ms` carries the structured measurement into
future JSON benchmark output. The earlier live artifact retains the same values inside
`ttft_status`, so no historical value was invented or lost.

## Live Claude Code Sonnet measurements

| Gold task | First observable assistant event | Validated result latency | Difference after first event |
|---|---:|---:|---:|
| Repository overview | 19,946.597 ms | 21,106.451 ms | 1,159.854 ms |
| Session architecture | 15,616.342 ms | 33,974.684 ms | 18,358.342 ms |
| Request lifecycle | 14,142.486 ms | 49,389.096 ms | 35,246.610 ms |
| Blueprint architecture | 6,293.164 ms | 18,135.381 ms | 11,842.217 ms |

Derived across four samples:

- First observable event p50: **14,879.414 ms**
- First observable event p95: **19,297.059 ms**
- End-to-end p50: **27,540.568 ms**
- End-to-end p95: **47,076.934 ms**

The first event includes Claude CLI launch, SDK transport setup, authentication, provider
queueing, model inference, structured-output processing before the event is emitted, and
any SDK buffering. It is therefore a valuable application responsiveness measurement,
but it cannot isolate network-only or model-only latency.

## OpenRouter comparison

Waypoint calls OpenRouter through SSE streaming and stops the TTFT timer at the first
meaningful `delta.content` or `delta.tool_calls` event. This is true stream TTFT.

The two valid OpenRouter responses measured:

- Session architecture: **2,201.151 ms**
- Blueprint architecture: **45,687.632 ms**
- TTFT p50: **23,944.391 ms**

The other two OpenRouter tasks failed post-response validation. Their total failure
latencies are retained, but they are not counted as valid TTFT samples in the summary.

## How to obtain true Claude token TTFT

There are only two defensible approaches:

1. Use the Anthropic Messages API with streaming enabled and timestamp the first content
   block or tool-input delta. This measures real API TTFT, but it measures the Anthropic
   API route—not the Claude Code subscription/Agent SDK route.
2. Adopt token/delta events if a future Claude Agent SDK version exposes them. Waypoint can
   attach the same timer to that official event without changing the benchmark definition.

Intercepting undocumented bundled-CLI stdout would be brittle. It may expose complete SDK
protocol messages rather than raw provider tokens, can change between SDK releases, and
would make the number look more precise than it is. It is not recommended for evaluated
evidence.

## Token-accounting context

Claude Code's live four-task run reported:

- Uncached input tokens: 12
- Cache-creation input tokens: 20,237
- Cache-read input tokens: 111,959
- Effective input tokens: 132,208
- Output tokens: 11,266

The tiny uncached-input value must not be presented as the complete input footprint. The
effective value deliberately includes cache creation and cache reads. OpenRouter reported
10,430 effective input tokens and 3,234 output tokens across its two valid responses.

## Reproduction

Start a dual-provider backend, analyze the Flask fixture, then run:

```powershell
.\.venv\Scripts\python.exe -m verification.scripts.model_quality_benchmark `
  --analysis-id <FLASK_ANALYSIS_ID> `
  --label claude-code-openrouter `
  --confirm-live-model-usage
```

Raw historical evidence:

- `verification/results/model-quality-claude-code-openrouter.json`
- `verification/results/model-quality-claude-code-openrouter.md`
- `.waypoint-data/evaluation-server.stdout.log`

