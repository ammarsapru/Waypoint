# Live multi-step agent demonstration

The live scenario demonstrates one agent-assisted developer-onboarding workflow:

1. Import and index Flask.
2. Ask how an incoming request reaches a view function.
3. Inspect the agent's repository tool calls and validated citations.
4. Ask a follow-up in the same conversation to prove persistent memory.
5. Generate a backend-specific reading route.
6. Generate a validated first-contribution mission.
7. Send the original question and one frozen evidence set to two models.

The application already emits hooks for agent rounds, model requests/responses, selected
tools, tool results, failures, token usage, provider routing, evidence files, and timing.
The script extracts those existing events into a focused JSONL artifact; it does not claim
to expose private model chain-of-thought.

Start the backend in dual-model mode with trace-file logging enabled, then run:

```powershell
.\.venv\Scripts\python.exe -m verification.scripts.live_multistep_demo `
  --confirm-live-model-usage
```

Outputs:

- `verification/results/live-multistep-report.json`
- `verification/results/live-multistep-trace.jsonl`

The explicit flag prevents accidental model spending in CI.

## Verified run

The July 24, 2026 run completed the full workflow against Flask:

- five generated onboarding-tour steps;
- one source-backed first-contribution mission;
- three repository-tool results in each of the two memory-linked Ask turns;
- 66 focused hook/log events;
- one same-question comparison using `sonnet` and `claude-fable-5`, with seven
  validated citations from each model and one shared evidence fingerprint.

The committed evidence is `verification/results/live-multistep-report.json` and
`verification/results/live-multistep-trace.jsonl`. OpenRouter's free daily quota was
exhausted during repeated validation, so the final full demonstration used two Claude Code
models. Live model tests remain deliberately opt-in because they consume provider quota.
