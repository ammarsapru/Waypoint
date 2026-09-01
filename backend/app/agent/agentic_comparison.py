from __future__ import annotations

import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

from backend.app.agent.comparison import ModelComparisonService
from backend.app.agent.provider import model_provider_router
from backend.app.agent.service import RepositoryAgentService
from backend.app.config import settings
from backend.app.graph.store import AnalysisSession
from backend.app.observability import trace_context, traced
from backend.app.onboarding.models import (
    AgenticComparisonReport,
    AgenticComparisonRequest,
    AgenticModelRun,
    GroundedQuestion,
    ModelComparisonFailure,
    RetrievalLabels,
    RetrievalStageMetrics,
)


def _canonical(path: str) -> str:
    return path.replace("\\", "/").lstrip("./").casefold()


def _dedupe(paths: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = _canonical(path)
        if key and key not in seen:
            seen.add(key)
            result.append(path.replace("\\", "/"))
    return result


def score_paths(paths: list[str], labels: RetrievalLabels) -> RetrievalStageMetrics:
    """Score one ranked retrieval stage against explicit, versioned human labels."""
    ranked = _dedupe(paths)
    normalized = [_canonical(path) for path in ranked]
    groups = [
        {_canonical(path) for path in group if path}
        for group in labels.required_path_groups if group
    ]
    acceptable = {_canonical(path) for path in labels.acceptable_paths if path}
    relevant = acceptable | set().union(*groups) if groups else acceptable
    forbidden = {_canonical(path) for path in labels.forbidden_paths if path}

    def relevant_at(k: int) -> list[int]:
        return [1 if path in relevant else 0 for path in normalized[:k]]

    hits5 = relevant_at(5)
    hits10 = relevant_at(10)
    first_rank = next((index for index, hit in enumerate(relevant_at(len(ranked)), 1) if hit), None)
    found_relevant = {path for path in normalized if path in relevant}
    group_hits = sum(bool(group.intersection(normalized)) for group in groups)
    dcg = sum(hit / math.log2(index + 2) for index, hit in enumerate(hits10))
    ideal_hits = min(len(relevant), 10)
    idcg = sum(1 / math.log2(index + 2) for index in range(ideal_hits))
    forbidden_hits = sum(path in forbidden for path in normalized)
    return RetrievalStageMetrics(
        ranked_paths=ranked,
        hit_at_5=float(any(hits5)),
        hit_at_10=float(any(hits10)),
        precision_at_10=(sum(hits10) / max(1, min(10, len(ranked)))),
        path_recall=(len(found_relevant) / len(relevant) if relevant else 0.0),
        required_group_recall=(group_hits / len(groups) if groups else 0.0),
        mrr=(1 / first_rank if first_rank else 0.0),
        ndcg_at_10=(dcg / idcg if idcg else 0.0),
        forbidden_hit_rate=(forbidden_hits / len(ranked) if ranked else 0.0),
    )


class _EndpointMessages:
    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        self.responses: list[Any] = []

    def create(self, **request: Any) -> Any:
        role = str(request.pop("_waypoint_role", "investigation"))
        response = model_provider_router.create_for_endpoint(
            self.provider,
            self.model,
            f"independent-agent-{role}",
            request,
        )
        self.responses.append(response)
        return response


class AgenticRetrievalComparisonService:
    """Compare two endpoints that independently choose and call Waypoint tools."""

    def __init__(self, session: AnalysisSession) -> None:
        self.session = session
        self._agents: dict[tuple[str, str], RepositoryAgentService] = {}

    def _run(
        self,
        endpoint: tuple[str, str],
        request: AgenticComparisonRequest,
        trace_id: str,
        agent: RepositoryAgentService,
    ) -> AgenticModelRun:
        with trace_context(trace_id):
            return self._run_traced(endpoint, request, trace_id, agent)

    @traced("agent.agentic_retrieval_comparison.endpoint")
    def _run_traced(
        self,
        endpoint: tuple[str, str],
        request: AgenticComparisonRequest,
        trace_id: str,
        agent: RepositoryAgentService,
    ) -> AgenticModelRun:
        provider, model = endpoint
        messages = _EndpointMessages(provider, model)
        client = SimpleNamespace(messages=messages)
        started = time.perf_counter()
        answer = agent.answer(
            GroundedQuestion(question=request.question, conversation_scope="ask"),
            client_override=client,
            ephemeral=True,
        )
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        candidates = _dedupe([
            path for activity in answer.tool_trace for path in activity.candidate_files
        ])
        explicit = _dedupe([
            path
            for activity in answer.tool_trace
            for path in activity.explicitly_inspected_files
        ])
        cited = _dedupe([citation.span.path for citation in answer.citations])
        input_parts: list[int] = []
        output_parts: list[int] = []
        costs: list[float] = []
        ttfts: list[float] = []
        statuses: list[str] = []
        actual_models: list[str] = []
        for response in messages.responses:
            usage = getattr(response, "usage", None)
            fresh = getattr(usage, "input_tokens", None)
            cache_create = getattr(usage, "cache_creation_input_tokens", None)
            cache_read = getattr(usage, "cache_read_input_tokens", None)
            input_parts.extend(
                int(value) for value in (fresh, cache_create, cache_read)
                if isinstance(value, (int, float))
            )
            output = getattr(usage, "output_tokens", None)
            if isinstance(output, (int, float)):
                output_parts.append(int(output))
            cost = getattr(response, "waypoint_cost_usd", None)
            if isinstance(cost, (int, float)):
                costs.append(float(cost))
            ttft = getattr(response, "waypoint_ttft_ms", None)
            if isinstance(ttft, (int, float)):
                ttfts.append(float(ttft))
            statuses.append(str(getattr(response, "waypoint_ttft_status", "unavailable")))
            actual = getattr(response, "waypoint_actual_model", None)
            if isinstance(actual, str) and actual not in actual_models:
                actual_models.append(actual)
        input_tokens = sum(input_parts) if input_parts else None
        output_tokens = sum(output_parts) if output_parts else None
        return AgenticModelRun(
            provider=provider,
            model=model,
            trace_id=trace_id,
            actual_models=actual_models,
            answer=answer.answer,
            citations=answer.citations,
            candidate_retrieval=score_paths(candidates, request.labels),
            explicit_inspection=score_paths(explicit, request.labels),
            cited_retrieval=score_paths(cited, request.labels),
            tool_trace=answer.tool_trace,
            repository_tool_calls=len(answer.tool_trace),
            invalid_tool_calls=sum(
                activity.status in {"input_rejected", "failed"}
                for activity in answer.tool_trace
            ),
            total_model_rounds=len(messages.responses),
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=(
                input_tokens + output_tokens
                if input_tokens is not None and output_tokens is not None else None
            ),
            estimated_cost_usd=sum(costs) if costs else None,
            ttft_ms=min(ttfts) if ttfts else None,
            ttft_status="; ".join(dict.fromkeys(statuses)) or "unavailable",
        )

    @traced("agent.agentic_retrieval_comparison.compare")
    def compare(self, request: AgenticComparisonRequest) -> AgenticComparisonReport:
        return self.compare_endpoints(request, ModelComparisonService._endpoints())

    def compare_endpoints(
        self,
        request: AgenticComparisonRequest,
        endpoints: list[tuple[str, str]],
        *,
        parallel: bool = True,
    ) -> AgenticComparisonReport:
        if not 2 <= len(endpoints) <= 4 or len(set(endpoints)) != len(endpoints):
            raise ValueError("Agentic comparison requires two to four distinct endpoints")
        runs: list[AgenticModelRun] = []
        failures: list[ModelComparisonFailure] = []
        # Build retrieval indexes sequentially before model threads start. SQLite WAL
        # initialization can otherwise race when both endpoint agents are constructed.
        agents: dict[tuple[str, str], RepositoryAgentService] = {}
        for endpoint in endpoints:
            agent = self._agents.get(endpoint)
            if agent is None:
                agent = RepositoryAgentService(self.session)
                self._agents[endpoint] = agent
            agents[endpoint] = agent
        workers = len(endpoints) if parallel else 1
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agentic-comparison") as pool:
            futures = {}
            for endpoint in endpoints:
                trace_id = f"agentic-{uuid.uuid4().hex}"
                futures[endpoint] = (
                    pool.submit(self._run, endpoint, request, trace_id, agents[endpoint]),
                    time.perf_counter(),
                    trace_id,
                )
            for endpoint, (future, started, trace_id) in futures.items():
                try:
                    runs.append(future.result())
                except Exception as exc:
                    failures.append(ModelComparisonFailure(
                        provider=endpoint[0],
                        model=endpoint[1],
                        trace_id=trace_id,
                        error_type=type(exc).__name__,
                        detail=str(exc)[:4000],
                        duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    ))
        return AgenticComparisonReport(
            analysis_id=self.session.id,
            question=request.question,
            labels=request.labels,
            runs=runs,
            failures=failures,
        )
