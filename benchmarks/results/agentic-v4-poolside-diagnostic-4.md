# V4 independent-agent evaluation report

## Run identity

- Protocol: `independent_agent_retrieval`
- Cases: 1
- Expected model attempts: 2
- Successful attempts: 0
- Failed attempts: 2
- Release-gate eligible: False

Each endpoint independently selected and called production Waypoint tools. No evidence bundle was frozen or shared between models.

## Per-model retrieval and efficiency

| Fixed endpoint | Successes | Candidate Hit@5 | Candidate recall | Candidate nDCG@10 | Cited recall | Concept recall | Mean tools | Mean rounds | Mean tokens | Mean seconds | p95 seconds |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|

## Results by repository

| Repository | Attempts | Candidate recall | Cited recall | Concept recall | Mean seconds |
|---|---:|---:|---:|---:|---:|

## Results by scenario category

| Category | Attempts | Candidate Hit@5 | Group recall | Concept recall |
|---|---:|---:|---:|---:|

## Provider and validation failures

| Case | Endpoint | Trace | Type | Detail |
|---|---|---|---|---|
| v4-wp-01 | `openrouter:poolside/laguna-m.1:free` | `agentic-d51da4c5609b4680ad0c0537c2948879` | AgentExecutionError | Model request failed: OpenRouter returned HTTP 429: {"code": 429, "message": "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day"} |
| v4-wp-01 | `openrouter:poolside/laguna-xs-2.1:free` | `agentic-c78dbdb172c74c8abfc70441c3d05cc5` | AgentExecutionError | Model request failed: OpenRouter returned HTTP 429: {"code": 429, "message": "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day"} |

## Hook and log quality

| Measurement | Result |
|---|---:|
| Hook events | 32 |
| Correlated traces | 2 |
| Trace correlation | 100.0% |
| Pre-to-terminal pairing | 100.0% |
| Incomplete spans | 0 |
| Orphan terminal spans | 0 |
| Possible secret leaks | 0 |
| Redaction markers | 0 |
| Serialized hook bytes | 22,654 |
| Bytes per event | 707.94 |
| Mean hook callback ms | 0.883763 |

## Interpretation limits

- Seed expected paths and concepts require independent human adjudication.
- Concept recall is deterministic phrase coverage, not a substitute for human factual review.
- Non-streaming provider calls report TTFT as unavailable rather than estimating it.
- Logs capture observable calls, arguments, bounded results, corrections, and failures; they do not expose private chain-of-thought.
