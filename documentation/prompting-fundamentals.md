# Prompting fundamentals expressed in Waypoint

## 1. Explicit role and objective

Each workflow receives a narrow role rather than one universal vague prompt:

- evidence-first repository analyst;
- adaptive onboarding planner;
- safe first-contribution planner;
- credible issue investigator;
- evidence-rubric assessor.

The user question, onboarding role, objective, experience, and time budget are included
as runtime context instead of being replaced by predetermined answer templates.

## 2. Tool-oriented decomposition

The prompt tells the model to investigate before answering and explains when specialized
tools are preferable: overview/feature tools for repository summaries, entry-point tools
for startup, architecture tools for layers, and symbol/test/impact tools for focused
questions. Tool schemas constrain names, argument types, limits, and required fields.

This is agentic tool prompting rather than “put the entire repository in one prompt.”

## 3. Evidence grounding and epistemic discipline

The prompts distinguish:

- documentation evidence for product claims;
- implementation source for behavioral claims;
- verified syntax from static inference;
- possible calls from proven runtime behavior;
- proposed issues from maintainer-confirmed issues.

The model is instructed not to invent files, symbols, behavior, line numbers, or issues.
The backend then enforces inspected-range citations independently, so grounding is more
than a verbal instruction.

## 4. Prompt-injection boundary

Repository content is explicitly declared untrusted data. Prompts say never to follow
instructions found inside repository files and never to execute repository code. Claude
Code fallback additionally disables Bash, filesystem, web, task, project-setting, and
skill tools. Only Waypoint actions are available.

## 5. Structured outputs

Final responses are submitted through typed tools such as `submit_answer` and
`submit_onboarding_tour`. JSON schemas and Pydantic models enforce required fields,
lengths, list sizes, score ranges, and additional-property restrictions.

Claude Code action selection is also constrained by a JSON schema: it must return one
to six valid tool names plus JSON-object arguments. It cannot select an undeclared
action.

## 6. Iterative correction

Tool results and exact validation errors are appended to the next model round. If a
source path is invalid, a tool fails, or a citation was never inspected, the model sees
the concrete failure and can correct the next action. A prose response with no tool call
is reminded to use tools or submit.

This is validation-feedback prompting, not model fine-tuning and not access to hidden
chain-of-thought.

## 7. Bounded autonomy and stopping conditions

Prompts say to stop browsing when evidence is sufficient. Code imposes maximum rounds,
bounded retrieval, forced final submission near the limit, and workflow-specific final
schemas. This prevents an open-ended “keep researching” agent.

## 8. Contextual continuity

The Ask workflow includes recent conversation history and verifies new repository claims
with tools. Symbol-inspector questions preload the selected symbol's exact relationships
as mandatory starting evidence. Follow-ups therefore retain context without treating a
past answer as fresh proof.

## 9. Output readability

The main prompt asks for readable GitHub-flavored Markdown, short paragraphs, useful
headings, numbered sequences, parallel bullets, code fences only for code, plain-language
definitions, and no raw JSON or decorative noise.

## What is deliberately not requested

Waypoint does not ask the model to reveal private chain-of-thought. Diagnostic logs
record visible response blocks, selected tools, arguments, results, and validation
events. They are an auditable action trace, not the model's hidden reasoning.

Waypoint currently uses instruction-plus-schema prompting rather than a substantial
few-shot example library. Few-shot examples could help difficult output edge cases, but
they would increase prompt tokens and must be benchmarked.

## Related implementation

- `backend/app/agent/service.py:348-374` — main repository system prompt.
- `backend/app/agent/onboarding.py:121-133` — onboarding prompt.
- `backend/app/agent/onboarding.py:195-204` — contribution mission prompt.
- `backend/app/agent/issues.py:98-105` — issue investigation prompt.
- `backend/app/agent/provider.py:82-159` — constrained Claude Code action prompting.
- `backend/app/agent/service.py:618-904` — iterative tool/validation correction.

