# Two-model task benchmark

Analysis: `6c89f44fbfc74835be24a7f6cd2f4c2a`

Both models received the same server-retrieved, frozen evidence for each task. They did not crawl the repository directly during this comparison.

| Task | Model | Input tokens | Output tokens | Total tokens | Time (ms) | TTFT (ms) | Tool calls | Output chars | Citations |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1. What is this repository about? Highlight its top 10 features. | claude-code / sonnet | 2 | 2367 | 2369 | 31551.435 | N/A | 1 | 2940 | 8 |
| 1. What is this repository about? Highlight its top 10 features. | claude-code / claude-fable-5 | 2 | 2566 | 2568 | 37865.659 | N/A | 1 | 3059 | 8 |
| 2. Which files are the main application entry points, and why? | claude-code / sonnet | 2 | 1258 | 1260 | 19063.8 | N/A | 1 | 1406 | 3 |
| 2. Which files are the main application entry points, and why? | claude-code / claude-fable-5 | 2 | 2115 | 2117 | 29192.817 | N/A | 1 | 2585 | 6 |
| 3. How is the backend organized from HTTP entry point to business logic? | claude-code / sonnet | 2 | 2340 | 2342 | 30589.065 | N/A | 1 | 3254 | 8 |
| 3. How is the backend organized from HTTP entry point to business logic? | claude-code / claude-fable-5 | 2 | 2385 | 2387 | 32236.967 | N/A | 1 | 2903 | 8 |
| 4. Trace one important cross-file call path and explain the evidence. | claude-code / sonnet | 2 | 2096 | 2098 | 34702.116 | N/A | 1 | 2108 | 4 |
| 4. Trace one important cross-file call path and explain the evidence. | claude-code / claude-fable-5 | 2 | 2240 | 2242 | 40194.509 | N/A | 1 | 2576 | 4 |

## Measurement limitations

- `Time` is end-to-end model-call latency for a complete non-streaming response.
- TTFT is `N/A`: the current providers do not stream this endpoint, so true time-to-first-token cannot be observed honestly.
- Each model makes one forced structured-output tool call. Repository retrieval is performed once by the server before fan-out.
- The application caps questions at 500 characters, evidence at 2–15 passages, and requested output at the configured `WAYPOINT_AGENT_MAX_OUTPUT_TOKENS` value.
- Provider-reported usage is recorded as returned. Claude Code subscription usage may report cache-adjusted or unexpectedly small input counts and should not be treated as raw prompt length.
