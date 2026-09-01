from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ContractModel(BaseModel):
    """Strict model-facing contract: unknown fields and implicit coercion are rejected."""

    model_config = ConfigDict(extra="forbid", strict=True)


class EmptyInput(ContractModel):
    pass


class TreeInput(ContractModel):
    prefix: str = ""
    limit: int = Field(default=300, ge=1, le=1000)


class SearchInput(ContractModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=12, ge=1, le=50)
    path_prefixes: list[str] = Field(default_factory=list, max_length=10)
    symbol_kinds: list[Literal["module", "class", "function", "method", "file"]] = Field(
        default_factory=list, max_length=5
    )
    languages: list[
        Literal[
            "python", "javascript", "typescript", "java", "documentation",
            "configuration",
        ]
    ] = Field(default_factory=list, max_length=6)
    include_tests: bool = True


class FindSymbolsInput(ContractModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=20, ge=1, le=100)
    symbol_kinds: list[Literal["module", "class", "function", "method"]] = Field(
        default_factory=list, max_length=4
    )
    languages: list[Literal["python", "javascript", "typescript", "java"]] = Field(
        default_factory=list, max_length=4
    )


class ReadSourceInput(ContractModel):
    path: str = Field(min_length=1)
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=200, ge=1)

    @model_validator(mode="after")
    def ordered_range(self) -> "ReadSourceInput":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class NodeInput(ContractModel):
    node_id: str = Field(min_length=1)


class GraphInput(NodeInput):
    depth: int = Field(default=1, ge=1, le=3)


class LimitInput(ContractModel):
    limit: int = Field(default=10, ge=1, le=20)


class EntryPointInput(ContractModel):
    limit: int = Field(default=15, ge=1, le=30)


class ArchitectureInput(ContractModel):
    limit_per_layer: int = Field(default=5, ge=1, le=10)


class PathInput(ContractModel):
    path: str = Field(min_length=1)


class RelatedTestsInput(ContractModel):
    query: str = ""
    node_id: str | None = None
    limit: int = Field(default=15, ge=1, le=30)

    @model_validator(mode="after")
    def has_selector(self) -> "RelatedTestsInput":
        if not self.query.strip() and not self.node_id:
            raise ValueError("query or node_id is required")
        return self


class ImpactInput(NodeInput):
    depth: int = Field(default=2, ge=1, le=4)


class DiagnosticsInput(ContractModel):
    limit: int = Field(default=20, ge=1, le=50)


class MappingOutput(BaseModel):
    """Base for server-owned JSON objects; extra fields remain visible to the model."""

    model_config = ConfigDict(extra="allow", strict=True)


class TreeOutput(MappingOutput):
    paths: list[str]
    total: int = Field(ge=0)
    truncated: bool


class SearchOutput(MappingOutput):
    results: list[dict[str, Any]]
    count: int = Field(ge=0)

    @model_validator(mode="after")
    def consistent_count(self) -> "SearchOutput":
        if self.count != len(self.results):
            raise ValueError("count does not match the number of results")
        return self


class SymbolSearchOutput(SearchOutput):
    query: str
    revision_id: str


class ReadSourceOutput(MappingOutput):
    path: str
    canonical_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    line_count: int = Field(ge=0)
    content: str


class GraphOutput(MappingOutput):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


class SymbolOutput(MappingOutput):
    node: dict[str, Any]
    neighbors: list[dict[str, Any]]
    edges: list[dict[str, Any]]


class OverviewOutput(MappingOutput):
    repository_name: str
    files_analyzed: int = Field(ge=0)
    evidence: list[dict[str, Any]]


class EvidenceOutput(MappingOutput):
    evidence: list[dict[str, Any]]


class IndexStatusOutput(MappingOutput):
    revision_id: str


class ToolInputValidationError(ValueError):
    def __init__(self, tool: str, error: ValidationError) -> None:
        self.tool = tool
        self.errors = error.errors(include_url=False, include_context=False)
        super().__init__(f"Invalid input for {tool}")


class ToolOutputValidationError(RuntimeError):
    def __init__(self, tool: str, error: ValidationError) -> None:
        self.tool = tool
        self.errors = error.errors(include_url=False, include_context=False)
        super().__init__(f"Waypoint produced an invalid result for {tool}")


@dataclass(frozen=True, slots=True)
class ToolContract:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]

    def provider_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": schema}

    def validate_input(self, value: Any) -> BaseModel:
        try:
            return self.input_model.model_validate(value, strict=True)
        except ValidationError as exc:
            raise ToolInputValidationError(self.name, exc) from exc

    def validate_output(self, value: Any) -> dict[str, Any]:
        try:
            parsed = self.output_model.model_validate(value, strict=True)
        except ValidationError as exc:
            raise ToolOutputValidationError(self.name, exc) from exc
        return parsed.model_dump(mode="json")


def correction_envelope(
    *, code: str, tool: str, message: str, errors: list[dict[str, Any]] | None = None,
    retryable: bool = True,
) -> str:
    """Stable machine-readable feedback returned to a model after validation fails."""

    return json.dumps({
        "ok": False,
        "error": {
            "code": code,
            "tool": tool,
            "message": message,
            "retryable": retryable,
            "violations": errors or [],
            "instruction": (
                "Correct only the invalid fields and call the tool again."
                if retryable else "Do not retry this tool; Waypoint must repair its server result."
            ),
        },
    }, ensure_ascii=False)


_DESCRIPTIONS = {
    "list_repository_tree": "List indexed files to discover documentation, entry points, configuration, tests, or packages.",
    "search_repository": "Search indexed code and documentation and return ranked excerpts with exact line ranges.",
    "find_symbols": "Find exact or partial class, function, method, and module names.",
    "get_index_status": "Get the active immutable repository revision and persisted index counts.",
    "read_source": "Read an indexed source range with line numbers; at most 250 lines are returned.",
    "inspect_symbol": "Inspect one graph symbol and its immediate relationships.",
    "expand_graph": "Expand graph relationships around a known node by one to three hops.",
    "get_repository_overview": "Get languages, frameworks, manifests, documentation, graph counts, central modules, and evidence.",
    "get_feature_evidence": "Get documentation plus diverse production symbols for feature questions.",
    "find_entry_points": "Find likely backend, frontend, CLI, and Java application entry points.",
    "get_backend_architecture": "Classify repository modules into architectural layers with evidence.",
    "get_file_structure": "Describe symbols and cross-file relationships for one file.",
    "get_symbol_relationships": "Get callers, callees, imports, importers, and evidence for one symbol.",
    "find_related_tests": "Find test files and test symbols related to a query or graph node.",
    "get_dependency_impact": "Trace bounded reverse import/call dependents for a symbol.",
    "get_project_configuration": "Read indexed package, build, environment-template, and container configuration.",
    "get_analysis_diagnostics": "Get parse diagnostics and bounded unresolved-reference samples.",
}


TOOL_CONTRACTS: dict[str, ToolContract] = {
    contract.name: contract for contract in (
        ToolContract("list_repository_tree", _DESCRIPTIONS["list_repository_tree"], TreeInput, TreeOutput),
        ToolContract("search_repository", _DESCRIPTIONS["search_repository"], SearchInput, SearchOutput),
        ToolContract("find_symbols", _DESCRIPTIONS["find_symbols"], FindSymbolsInput, SymbolSearchOutput),
        ToolContract("get_index_status", _DESCRIPTIONS["get_index_status"], EmptyInput, IndexStatusOutput),
        ToolContract("read_source", _DESCRIPTIONS["read_source"], ReadSourceInput, ReadSourceOutput),
        ToolContract("inspect_symbol", _DESCRIPTIONS["inspect_symbol"], NodeInput, SymbolOutput),
        ToolContract("expand_graph", _DESCRIPTIONS["expand_graph"], GraphInput, GraphOutput),
        ToolContract("get_repository_overview", _DESCRIPTIONS["get_repository_overview"], EmptyInput, OverviewOutput),
        ToolContract("get_feature_evidence", _DESCRIPTIONS["get_feature_evidence"], LimitInput, EvidenceOutput),
        ToolContract("find_entry_points", _DESCRIPTIONS["find_entry_points"], EntryPointInput, EvidenceOutput),
        ToolContract("get_backend_architecture", _DESCRIPTIONS["get_backend_architecture"], ArchitectureInput, EvidenceOutput),
        ToolContract("get_file_structure", _DESCRIPTIONS["get_file_structure"], PathInput, EvidenceOutput),
        ToolContract("get_symbol_relationships", _DESCRIPTIONS["get_symbol_relationships"], NodeInput, EvidenceOutput),
        ToolContract("find_related_tests", _DESCRIPTIONS["find_related_tests"], RelatedTestsInput, EvidenceOutput),
        ToolContract("get_dependency_impact", _DESCRIPTIONS["get_dependency_impact"], ImpactInput, EvidenceOutput),
        ToolContract("get_project_configuration", _DESCRIPTIONS["get_project_configuration"], EmptyInput, EvidenceOutput),
        ToolContract("get_analysis_diagnostics", _DESCRIPTIONS["get_analysis_diagnostics"], DiagnosticsInput, EvidenceOutput),
    )
}

REPOSITORY_TOOL_SCHEMAS = [contract.provider_schema() for contract in TOOL_CONTRACTS.values()]
