# V4 independent-agent evaluation report

## Run identity

- Protocol: `independent_agent_retrieval`
- Cases: 50
- Expected model attempts: 100
- Successful attempts: 50
- Failed attempts: 50
- Release-gate eligible: False

Each endpoint independently selected and called production Waypoint tools. No evidence bundle was frozen or shared between models.

## Per-model retrieval and efficiency

| Fixed endpoint | Successes | Candidate Hit@5 | Candidate recall | Candidate nDCG@10 | Cited recall | Concept recall | Mean tools | Mean rounds | Mean tokens | Mean seconds | p95 seconds |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `bedrock:us.anthropic.claude-sonnet-4-6` | 49/49 | 10.2% | 77.8% | 9.5% | 62.9% | 72.9% | 11.63 | 5.96 | 218,178.65 | 100.56 | 131.67 |
| `bedrock:openai.gpt-oss-120b-1:0` | 1/1 | 100.0% | 25.0% | 39.0% | 25.0% | 0.0% | 2 | 6 | 42,651 | 62.30 | 62.30 |

## Results by repository

| Repository | Attempts | Candidate recall | Cited recall | Concept recall | Mean seconds |
|---|---:|---:|---:|---:|---:|
| flask | 10 | 70.2% | 54.8% | 79.5% | 86.22 |
| open-notebook | 9 | 66.7% | 61.9% | 73.3% | 110.33 |
| praw | 10 | 93.0% | 83.5% | 86.0% | 97.66 |
| waypoint | 21 | 76.5% | 55.6% | 59.8% | 102.76 |

## Results by scenario category

| Category | Attempts | Candidate Hit@5 | Group recall | Concept recall |
|---|---:|---:|---:|---:|
| ask-graph | 1 | 0.0% | 50.0% | 100.0% |
| ask-ui | 1 | 0.0% | 75.0% | 20.0% |
| audio-pipeline | 1 | 0.0% | 100.0% | 100.0% |
| blueprint-static | 1 | 0.0% | 100.0% | 80.0% |
| citation-recovery | 1 | 0.0% | 100.0% | 80.0% |
| cli-discovery | 1 | 0.0% | 100.0% | 75.0% |
| configuration | 1 | 0.0% | 100.0% | 80.0% |
| configuration-flow | 1 | 0.0% | 75.0% | 40.0% |
| content-import | 1 | 100.0% | 100.0% | 100.0% |
| context-proxies | 1 | 0.0% | 100.0% | 100.0% |
| credential-security | 1 | 0.0% | 100.0% | 60.0% |
| database-migrations | 1 | 0.0% | 75.0% | 80.0% |
| debug-errors | 1 | 0.0% | 66.7% | 80.0% |
| deployment | 1 | 100.0% | 50.0% | 100.0% |
| ecosystem-onboarding | 1 | 0.0% | 100.0% | 100.0% |
| explore-ui | 1 | 0.0% | 100.0% | 60.0% |
| external-dependencies | 1 | 0.0% | 100.0% | 100.0% |
| frontend-api | 1 | 0.0% | 100.0% | 100.0% |
| hybrid-retrieval | 1 | 0.0% | 100.0% | 100.0% |
| issue-automation | 1 | 0.0% | 100.0% | 75.0% |
| issues | 1 | 0.0% | 100.0% | 80.0% |
| knowledge-governance | 1 | 0.0% | 100.0% | 60.0% |
| learning-audits | 1 | 0.0% | 100.0% | 100.0% |
| limits | 1 | 0.0% | 75.0% | 16.7% |
| linkedin-automation | 1 | 0.0% | 100.0% | 75.0% |
| mcp | 1 | 0.0% | 100.0% | 20.0% |
| memory | 1 | 0.0% | 66.7% | 40.0% |
| model-comparison | 1 | 0.0% | 100.0% | 83.3% |
| model-discovery | 1 | 0.0% | 80.0% | 80.0% |
| observability | 1 | 0.0% | 100.0% | 66.7% |
| onboarding | 1 | 0.0% | 100.0% | 80.0% |
| podcast-generation | 1 | 0.0% | 100.0% | 80.0% |
| polyglot-graph | 1 | 0.0% | 100.0% | 83.3% |
| provider-recovery | 1 | 0.0% | 100.0% | 60.0% |
| quality-gates | 1 | 0.0% | 75.0% | 80.0% |
| request-security | 1 | 0.0% | 80.0% | 80.0% |
| responses | 1 | 0.0% | 100.0% | 75.0% |
| revision-index | 1 | 0.0% | 100.0% | 80.0% |
| routing | 1 | 0.0% | 100.0% | 100.0% |
| secure-import | 1 | 0.0% | 75.0% | 40.0% |
| session-format | 1 | 0.0% | 100.0% | 80.0% |
| skill-lifecycle | 1 | 100.0% | 75.0% | 100.0% |
| source-ingestion | 1 | 0.0% | 100.0% | 40.0% |
| source-safety | 1 | 0.0% | 66.7% | 66.7% |
| templates | 1 | 0.0% | 100.0% | 75.0% |
| testing-context | 1 | 100.0% | 100.0% | 50.0% |
| tool-contracts | 2 | 50.0% | 50.0% | 0.0% |
| tutorial-pipeline | 1 | 100.0% | 100.0% | 100.0% |
| ubd-validation | 1 | 0.0% | 100.0% | 50.0% |

## Provider and validation failures

| Case | Endpoint | Trace | Type | Detail |
|---|---|---|---|---|
| v4-wp-01 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-bb8070e5736840cebe7a29644f53fa1e` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-wp-03 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-bd0f5a8cde6a4f82b4e8fc5a431907cb` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-wp-04 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-99655caa0cf240c8b5a6693d33eebf1f` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-wp-05 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-a9cdb1eade864624b966892daf0d9289` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-wp-06 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-b974fa03a80d4fbf8439c858ecd7e881` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-wp-07 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-9368212987b54d3aafe42d7eb9b862c8` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-wp-08 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-84ab2b0ee3564f6fac15c022d69a4540` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-wp-09 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-70aeb1bd927f4feeb2e552c5edac54fb` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-wp-10 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-966fda0af68a4145ae65fd4f960a0388` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-wp-11 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-550bfcf7e0094460bd6d06fb473f5b60` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-wp-12 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-518eeda081b943d08152daedc7008880` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-wp-13 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-347a013c8a0d4ab5a04fdaa7e592df77` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-wp-14 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-84d67eb86d35422eb526fc7ba034bc30` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-wp-15 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-0d40763efb4c46648b1500090c468684` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-wp-16 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-8e053c8714ad425ab20736aed186c75a` | AgentExecutionError | Repository agent exceeded the tool-schema correction limit (2) |
| v4-wp-17 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-92e79ab6d45a42f191f745b62d0aed24` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-wp-18 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-66aaf5ff85d541de832cb92d789e6e35` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-wp-19 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-563f2a5823974a57a0acdc7c9fbeb151` | AgentExecutionError | Repository agent exceeded the tool-schema correction limit (2) |
| v4-wp-20 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-a717ed71fcc84bb289ba472cc823d414` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-fl-01 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-8a830e979e6a4da58ec896583f324811` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-fl-02 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-d24fb9f993294a929f87edcb036a461a` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-fl-03 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-ae6c8300402d4f6a96d878b2be7f8626` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-fl-04 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-bdfaff1575b9457d89fa7b149e930173` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-fl-05 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-d96a4d1e0507419b88a15fa2200da810` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-fl-06 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-499cff8c536949d8b05a9a75ea6d6785` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-fl-07 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-f7e0d563ac8d439abe74c6e64122be46` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-fl-08 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-1b8b0c7fb3174bf8b400810cdc5ac07f` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-fl-09 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-7d92e04c59bc415da33c48b3aec45637` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-fl-10 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-42d693ff92c34ffaaa0fd634976c48df` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-pr-01 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-7952e5fc3aec4df3a1f23edac343e430` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-pr-02 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-24738b75a5fa4ffdb551d6e07028f11d` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-pr-03 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-c772f54a7beb4b80ba061c88e55719b1` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-pr-04 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-004283c70d4b48b6aa0b61d41d30f8f5` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-pr-05 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-8d0084e71aa44d3cb56bf4f43fcf15a1` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-pr-06 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-556d8f7ede154d5dbf471bfef1d0ef9f` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-pr-07 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-f328f555c8df4e6b9f81c1a658c26624` | AgentExecutionError | Repository agent exceeded the tool-schema correction limit (2) |
| v4-pr-08 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-bdecb4f046294da6b6ddc2db749662bb` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-pr-09 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-2a6f7fda37db47628e7343013baa58d9` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-pr-10 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-8b6eba23ad7a46de824a002a6a406fbc` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-on-01 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-11b76e5bbe12456b8c6a4f78e6be04ed` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-on-02 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-24d3374d1ed04409b8f8ee66e0782eb4` | AgentExecutionError | Repository agent exceeded the tool-schema correction limit (2) |
| v4-on-03 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-4542ad90834e4681854d08d6357860b1` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-on-04 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-705dabc1b49f477e8aed71de0f11ca82` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-on-05 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-c21c522bdf7743f59c5e3e705a8d2f78` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-on-06 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-da6b6964e0194afabf752bb3a7eb16b5` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-on-07 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-ff3e0fc5670444909f76456a06add41e` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-on-08 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-8a6cb37f5b3a49269469cd9fba0b1138` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-on-09 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-89b8b4ca8169474090fa7f06458d5376` | AgentExecutionError | Repository synthesis exceeded 2 attempts |
| v4-on-10 | `bedrock:us.anthropic.claude-sonnet-4-6` | `agentic-ae471697fab2404ebc2bd64c0d4bc902` | AgentExecutionError | Model request failed: Bedrock Converse request failed for model us.anthropic.claude-sonnet-4-6: An error occurred (ThrottlingException) when calling the Converse operation (reached max retries: 4): Too many tokens per day, please wait before trying again. |
| v4-on-10 | `bedrock:openai.gpt-oss-120b-1:0` | `agentic-3e0220d1eef24f0db1fc30b204a6b660` | AgentExecutionError | Repository synthesis exceeded 2 attempts |

## Hook and log quality

| Measurement | Result |
|---|---:|
| Hook events | 6,443 |
| Correlated traces | 100 |
| Trace correlation | 100.0% |
| Pre-to-terminal pairing | 100.0% |
| Incomplete spans | 0 |
| Orphan terminal spans | 0 |
| Possible secret leaks | 26 |
| Redaction markers | 1,660 |
| Serialized hook bytes | 30,611,117 |
| Bytes per event | 4,751.07 |
| Mean hook callback ms | 1.731470 |

## Interpretation limits

- Seed expected paths and concepts require independent human adjudication.
- Concept recall is deterministic phrase coverage, not a substitute for human factual review.
- Non-streaming provider calls report TTFT as unavailable rather than estimating it.
- Logs capture observable calls, arguments, bounded results, corrections, and failures; they do not expose private chain-of-thought.
