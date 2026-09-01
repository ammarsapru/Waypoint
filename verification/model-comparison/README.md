# Same-question two-model comparison

There are two different multi-model concepts:

1. **Role routing:** an investigation model chooses repository tools and a synthesis model
   writes the final response. This is the normal Waypoint architecture.
2. **Direct comparison:** two models receive the same question and byte-identical frozen
   evidence, then answer independently. This is the Week 7 comparison deliverable.

Waypoint implements both. The comparison endpoint is:

```text
POST /api/v1/analyses/{analysis_id}/answer/compare
```

Example body:

```json
{
  "question": "How does Flask route a request to a view function?",
  "evidence_limit": 8
}
```

The service retrieves evidence once, hashes it, and sends the same serialized evidence to
the configured investigation and synthesis endpoints. Each answer is independently schema
validated, and every citation must fit inside the frozen evidence ranges. The report
includes providers, models, latency, tokens, estimated cost when available, citations,
validation status, evidence files, and the shared SHA-256 evidence fingerprint.

This endpoint requires `WAYPOINT_MODEL_ARCHITECTURE=dual` and two distinct configured
provider/model pairs.
w
The July 24, 2026 live run compared `sonnet` with `claude-fable-5`. Both received the
evidence fingerprint
`f1d915726ea3727d48adf652d73e27023014216ac98c94e72bd26ad4f52f0117`, passed citation
validation, and returned seven citations. See
`verification/results/live-multistep-report.json` for the complete metering and timing
record.
