from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path

from backend.app.config import settings
from backend.app.graph.models import GraphNode, NodeKind, SourceSpan
from backend.app.graph.store import AnalysisSession
from backend.app.observability import log_event, traced
from backend.app.onboarding.models import (
    GroundedAnswer,
    GroundedCitation,
    GroundedFeature,
    GroundedQuestion,
)
from backend.app.onboarding.service import OnboardingService

logger = logging.getLogger(__name__)

_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_HTML_TAG = re.compile(r"<[^>]+>")
_BOLD_FEATURE = re.compile(
    r"^\s*[-*]\s+\*\*(.+?)\*\*\s*(?:—|–|-|:)\s*(.+?)\s*$"
)
_OVERVIEW_PHRASES = (
    "what is this repository",
    "what is this repo",
    "repository about",
    "repo about",
    "top 10",
    "top ten",
    "highlight its",
    "main features",
    "key features",
    "capabilities",
)


def _plain_markdown(value: str) -> str:
    cleaned = _MARKDOWN_LINK.sub(r"\1", value)
    cleaned = _HTML_TAG.sub(" ", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" \t#>-")


class RepositoryQuestionService:
    def __init__(self, session: AnalysisSession) -> None:
        self.session = session
        self.report = session.report
        self.nodes = {node.id: node for node in self.report.nodes}

    def _read_lines(self, path: str) -> list[str]:
        if path not in self.session.source_paths:
            return []
        resolved = (self.session.root / Path(path)).resolve()
        try:
            resolved.relative_to(self.session.root)
        except ValueError:
            return []
        try:
            if resolved.stat().st_size > settings.max_python_file_bytes:
                return []
            return resolved.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return []

    def _readme_path(self) -> str | None:
        candidates = [
            path
            for path in self.session.source_paths
            if Path(path).name.lower() in {"readme.md", "readme.mdx", "readme.txt"}
        ]
        return min(
            candidates,
            key=lambda path: (len(Path(path).parts), len(path), path.lower()),
            default=None,
        )

    def _is_overview_question(self, question: str) -> bool:
        normalized = question.lower()
        if normalized.lstrip().startswith(("where ", "how is ", "how does ")):
            return False
        return any(phrase in normalized for phrase in _OVERVIEW_PHRASES)

    def _section(
        self, lines: list[str], heading_pattern: str
    ) -> tuple[int, int] | None:
        pattern = re.compile(heading_pattern, re.IGNORECASE)
        start = next(
            (index for index, line in enumerate(lines) if pattern.search(line)),
            None,
        )
        if start is None:
            return None
        end = next(
            (
                index
                for index in range(start + 1, len(lines))
                if re.match(r"^\s*##\s+", lines[index])
            ),
            len(lines),
        )
        return start, end

    def _summary(
        self, lines: list[str], section: tuple[int, int] | None
    ) -> tuple[str, int]:
        start, end = section or (0, min(len(lines), 100))
        paragraphs: list[str] = []
        current: list[str] = []
        summary_line = start + 1
        for index in range(start + 1, end):
            line = lines[index].strip()
            if line.startswith(("- ", "* ", "### ", "---")):
                if current:
                    paragraphs.append(_plain_markdown(" ".join(current)))
                break
            plain = _plain_markdown(line)
            if not plain:
                if current:
                    paragraphs.append(_plain_markdown(" ".join(current)))
                    current = []
                continue
            if not current:
                summary_line = index + 1
            current.append(plain)
            if len(" ".join(current)) > 500:
                break
        if current:
            paragraphs.append(_plain_markdown(" ".join(current)))
        usable = [paragraph for paragraph in paragraphs if len(paragraph) > 20]
        summary = " ".join(usable[:2]).strip()
        if not summary:
            summary = (
                f"{self.report.repository_name} is a software repository with "
                f"{self.report.stats.files_parsed} analyzed modules."
            )
        return summary[:1200], summary_line

    def _features(
        self,
        lines: list[str],
        primary_section: tuple[int, int] | None,
        limit: int = 10,
    ) -> list[GroundedFeature]:
        ranges = [primary_section] if primary_section else []
        feature_section = self._section(lines, r"^\s*##\s+Features\b")
        if feature_section and feature_section not in ranges:
            ranges.append(feature_section)
        ranges.append((0, len(lines)))
        features: list[GroundedFeature] = []
        seen: set[str] = set()
        for section in ranges:
            if section is None:
                continue
            start, end = section
            for index in range(start, end):
                match = _BOLD_FEATURE.match(lines[index])
                if not match:
                    continue
                title = _plain_markdown(match.group(1))
                description = _plain_markdown(match.group(2))
                key = title.lower()
                if not title or not description or key in seen:
                    continue
                seen.add(key)
                features.append(
                    GroundedFeature(
                        title=title,
                        description=description,
                        source_path="",
                        source_line=index + 1,
                    )
                )
                if len(features) >= limit:
                    return features
        return features

    def _implementation_citations(self, limit: int = 6) -> list[GroundedCitation]:
        degree: Counter[str] = Counter()
        for edge in self.report.edges:
            degree[edge.source] += 1
            degree[edge.target] += 1
        modules = [
            node
            for node in self.report.nodes
            if node.kind == NodeKind.MODULE
            and node.span
            and not any(
                marker in node.qualified_name.lower()
                for marker in ("test", "fixture", "conftest", "benchmark")
            )
        ]
        ranked = sorted(
            modules,
            key=lambda node: (
                -sum(
                    marker in node.qualified_name.lower()
                    for marker in ("main", "app", "api", "service", "backend")
                ),
                -degree[node.id],
                node.qualified_name,
            ),
        )
        return [
            GroundedCitation(
                node_id=node.id,
                qualified_name=node.qualified_name,
                kind=node.kind.value,
                span=node.span,
                title=node.name,
                excerpt=node.signature or node.qualified_name,
                relevance="Implementation entry point selected from the dependency graph.",
            )
            for node in ranked[:limit]
            if node.span
        ]

    @traced("qa.repository_overview")
    def _overview_answer(self, request: GroundedQuestion) -> GroundedAnswer:
        readme_path = self._readme_path()
        if not readme_path:
            return GroundedAnswer(
                question=request.question,
                answer=(
                    "I could not find a repository README, so I cannot provide "
                    "a reliable product overview or top-feature list."
                ),
                citations=[],
                refused=True,
                basis="Repository overview questions require documentation evidence.",
                answer_type="overview",
                provider="grounded-static",
            )
        lines = self._read_lines(readme_path)
        what_section = self._section(lines, r"^\s*##\s+What is\b")
        summary, summary_line = self._summary(lines, what_section)
        features = [
            feature.model_copy(update={"source_path": readme_path})
            for feature in self._features(lines, what_section)
        ]
        citations = [
            GroundedCitation(
                qualified_name=readme_path,
                kind="documentation",
                span=SourceSpan(
                    path=readme_path,
                    start_line=summary_line,
                    start_column=0,
                    end_line=summary_line,
                    end_column=0,
                ),
                title="Repository overview",
                excerpt=summary,
                relevance="Primary project description from repository documentation.",
            )
        ]
        citations.extend(
            GroundedCitation(
                qualified_name=readme_path,
                kind="documentation",
                span=SourceSpan(
                    path=readme_path,
                    start_line=feature.source_line,
                    start_column=0,
                    end_line=feature.source_line,
                    end_column=0,
                ),
                title=feature.title,
                excerpt=feature.description,
                relevance="Feature stated by the repository documentation.",
            )
            for feature in features
        )
        manifest = next(
            (
                path
                for path in ("pyproject.toml", "package.json", "Cargo.toml")
                if path in self.session.source_paths
            ),
            None,
        )
        if manifest:
            manifest_lines = self._read_lines(manifest)
            citations.append(
                GroundedCitation(
                    qualified_name=manifest,
                    kind="manifest",
                    span=SourceSpan(
                        path=manifest,
                        start_line=1,
                        start_column=0,
                        end_line=max(1, min(20, len(manifest_lines))),
                        end_column=0,
                    ),
                    title="Project manifest",
                    excerpt="Project metadata, scripts, and workspace configuration.",
                    relevance="Corroborates the repository's application structure.",
                )
            )
        citations.extend(self._implementation_citations())
        feature_text = "\n".join(
            f"{index}. {feature.title} — {feature.description} "
            f"[{readme_path}:L{feature.source_line}]"
            for index, feature in enumerate(features, start=1)
        )
        answer = (
            f"{summary} [{readme_path}:L{summary_line}]"
            + (
                f"\n\nTop {len(features)} documented features:\n{feature_text}"
                if features
                else "\n\nThe README did not contain a structured feature list."
            )
        )
        log_event(
            logger,
            logging.INFO,
            "qa.repository_overview_completed",
            "Repository overview generated from documentation and graph evidence",
            analysis_id=self.session.id,
            readme_path=readme_path,
            feature_count=len(features),
            citation_count=len(citations),
        )
        return GroundedAnswer(
            question=request.question,
            answer=answer,
            summary=summary,
            features=features,
            citations=citations,
            refused=False,
            basis=(
                "Repository documentation supplies the product claims; manifests "
                "and central production modules supply implementation context."
            ),
            answer_type="overview",
            suggested_questions=[
                "Which files implement the primary application entry points?",
                "How is the backend organized?",
                "Which modules have the largest dependency blast radius?",
            ],
            provider="grounded-static",
        )

    @traced("qa.contextual_answer")
    def answer(self, request: GroundedQuestion) -> GroundedAnswer:
        if self._is_overview_question(request.question):
            return self._overview_answer(request)
        answer = OnboardingService(self.report).answer(request)
        enriched: list[GroundedCitation] = []
        for citation in answer.citations:
            lines = self._read_lines(citation.span.path)
            excerpt = (
                lines[citation.span.start_line - 1].strip()
                if len(lines) >= citation.span.start_line
                else ""
            )
            enriched.append(
                citation.model_copy(
                    update={
                        "title": citation.qualified_name,
                        "excerpt": excerpt,
                        "relevance": "Symbol name matched the question terms.",
                    }
                )
            )
        return answer.model_copy(
            update={
                "citations": enriched,
                "answer_type": "symbol",
                "suggested_questions": [
                    "Show the files that call this symbol.",
                    "Which module owns this behavior?",
                ],
                "provider": "grounded-static",
            }
        )
