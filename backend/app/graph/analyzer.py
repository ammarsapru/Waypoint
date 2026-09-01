from __future__ import annotations

import ast
import hashlib
import logging
import os
import time
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.app.config import settings
from backend.app.graph.models import (
    AnalysisDiagnostic,
    AnalysisReport,
    AnalysisStats,
    EdgeKind,
    Evidence,
    EvidenceStatus,
    GraphEdge,
    GraphNode,
    NodeKind,
    SourceSpan,
    UnresolvedReference,
)
from backend.app.graph.polyglot import PolyglotGraphBuilder, SUPPORTED_EXTENSIONS
from backend.app.observability import log_event, traced

logger = logging.getLogger(__name__)

_SKIPPED_DIRECTORIES = {
    ".codex",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".waypoint-clones",
    ".waypoint-data",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "venv",
}


def _stable_id(*parts: object) -> str:
    """Derives a short, deterministic 24-char hex id from any number of values.

    Input: any positional args (str, int, etc.); each is stringified and joined
    with the ASCII unit-separator (\\x1f) to avoid delimiter collisions, then
    SHA-256 hashed. Used throughout analyzer.py/polyglot.py to build stable
    GraphNode/GraphEdge ids (e.g. _stable_id(path, "module", name)) so the same
    inputs always produce the same id across re-analysis runs, which lets the
    graph store diff/update incrementally instead of rebuilding from scratch.
    """
    material = "\x1f".join(str(part) for part in parts)  # join with a delimiter unlikely to appear in real values
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _span(relative_path: str, node: ast.AST) -> SourceSpan:
    """Builds a 1-indexed SourceSpan from a Python AST node's location attributes.

    Input: the file's `relative_path` string and any `ast.AST` node; reads
    `lineno`/`col_offset`/`end_lineno`/`end_col_offset` defensively via
    `getattr` with safe fallbacks, since some synthetic/older AST nodes lack
    them. Called throughout DefinitionVisitor and RelationshipVisitor (and by
    RepositoryAnalyzer.analyze) wherever a GraphNode or Evidence needs to record
    where in the source file it came from.
    """
    start_line = max(1, getattr(node, "lineno", 1))
    start_column = max(0, getattr(node, "col_offset", 0))
    end_line = max(start_line, getattr(node, "end_lineno", start_line))
    end_column = max(0, getattr(node, "end_col_offset", start_column))
    return SourceSpan(
        path=relative_path,
        start_line=start_line,
        start_column=start_column,
        end_line=end_line,
        end_column=end_column,
    )


def _module_name(relative_path: Path) -> str:
    """Converts a file path (relative to an import root) into a dotted Python module name.

    Input: a `pathlib.Path` such as `pkg/sub/__init__.py` or `pkg/mod.py`,
    already relative to whichever import root applies (see `_source_roots`/
    `_module_relative_path`). Strips the file suffix, drops a trailing
    `__init__` package marker, and joins the remaining parts with dots (falling
    back to `"__root__"` for a top-level `__init__.py`). Called by
    RepositoryAnalyzer._parse_file right after computing the module-relative
    path; this canonical name is what symbol lookup, import resolution, and
    qualified names key off throughout the rest of analysis.
    """
    without_suffix = relative_path.with_suffix("")
    parts = list(without_suffix.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else "__root__"


def _source_roots(repository_root: Path) -> tuple[Path, ...]:
    """Return import roots in priority order for common Python layouts.

    Input: the repository's absolute root Path. Prefers a `src/` subdirectory
    (the "src layout") if one exists, then falls back to the repository root
    itself, so both layouts resolve to the same import-style module names.
    Called once by RepositoryAnalyzer.analyze to build the `import_roots` tuple
    passed into every `_parse_file`/`_module_relative_path` call for that scan.
    """
    candidates: list[Path] = []
    src = repository_root / "src"
    if src.is_dir():
        candidates.append(src.resolve())
    candidates.append(repository_root.resolve())
    return tuple(candidates)


def _module_relative_path(
    repository_root: Path,
    source_path: Path,
    import_roots: tuple[Path, ...],
) -> Path:
    """Expresses a source file's path relative to whichever import root actually contains it.

    Input: the repository's `repository_root`, the file's absolute
    `source_path`, and the `import_roots` tuple from `_source_roots` (checked in
    priority order). Falls back to a path relative to `repository_root` if none
    of the import roots contain the file. Called by
    RepositoryAnalyzer._parse_file before `_module_name`, so module names are
    computed the way Python's own import system would resolve them (e.g.
    honoring a `src/` layout) rather than always being relative to the repo
    root.
    """
    resolved = source_path.resolve()
    for import_root in import_roots:
        try:
            return resolved.relative_to(import_root)
        except ValueError:
            continue
    return resolved.relative_to(repository_root.resolve())


def _syntax(node: ast.AST) -> str:
    """Renders an AST node back into readable source-like text for use as Evidence.syntax.

    Input: any `ast.AST` node; tries `ast.unparse(node)` and falls back to the
    node's type name (e.g. `"Call"`) if unparsing raises. Called throughout
    RelationshipVisitor (`_import_edge`, `visit_Call`, `_record_unresolved`) to
    populate the human-readable snippet stored on each Evidence record, so
    unparse failures on unusual/partial nodes degrade gracefully instead of
    crashing the whole analysis.
    """
    try:
        return ast.unparse(node)
    except Exception:
        return type(node).__name__


@dataclass(slots=True)
class ParsedModule:
    """Holds one Python module's parsed AST plus the import bookkeeping built up while visiting it.

    Created by RepositoryAnalyzer._parse_file (with `tree` populated and the
    import maps empty) and later mutated by RelationshipVisitor as it walks the
    tree, recording which local names refer to internal modules
    (`imported_modules`) or internal symbols (`imported_symbols`). Kept in the
    `modules` dict inside RepositoryAnalyzer.analyze for the lifetime of one
    repository scan so both the definition and relationship passes can share
    it.
    """

    path: Path
    relative_path: str
    module_name: str
    tree: ast.Module | None
    module_node_id: str
    imported_modules: dict[str, str] = field(default_factory=dict)
    imported_symbols: dict[str, tuple[str, str]] = field(default_factory=dict)


class GraphAccumulator:
    """Mutable sink for the nodes, edges, unresolved references, and diagnostics produced during one analysis run.

    Shared by both the Python pipeline (DefinitionVisitor, RelationshipVisitor)
    in this module and the polyglot pipeline (PolyglotGraphBuilder) so that
    Python and non-Python extraction write into the same graph. Instantiated
    once per call to RepositoryAnalyzer.analyze and drained into an
    AnalysisReport at the end of that method.
    """

    def __init__(self, max_unresolved_call_details: int) -> None:
        """Initializes empty node/edge/unresolved collections, capping how many unresolved *call* details will be retained."""
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[str, GraphEdge] = {}
        self.unresolved: list[UnresolvedReference] = []
        self.unresolved_total = 0
        self.stored_unresolved_calls = 0
        self.max_unresolved_call_details = max_unresolved_call_details
        self.diagnostics: list[AnalysisDiagnostic] = []

    @traced("graph.add_node")
    def add_node(self, node: GraphNode) -> None:
        """Registers (or overwrites) a GraphNode in the accumulator, keyed by its stable id, and logs a debug event.

        Input: a `GraphNode` instance. Called from dozens of call sites across
        analyzer.py (DefinitionVisitor._add_definition, module/repository node
        creation in RepositoryAnalyzer) and polyglot.py (PolyglotGraphBuilder)
        every time a definition is discovered. Overwriting by id (rather than
        erroring on duplicates) is what makes `_stable_id`-based ids safe to
        re-add across re-parses of the same file.
        """
        self.nodes[node.id] = node
        log_event(
            logger,
            logging.DEBUG,
            "graph.node_added",
            "Graph node added",
            node_id=node.id,
            kind=node.kind,
            qualified_name=node.qualified_name,
            span=node.span,
        )

    @traced("graph.add_edge")
    def add_edge(self, edge: GraphEdge) -> None:
        """Registers (or overwrites) a GraphEdge in the accumulator, keyed by its stable id, and logs a debug event.

        Input: a `GraphEdge` instance. Called wherever a CONTAINS, IMPORTS,
        MAY_CALL, or INSTANTIATES relationship is discovered, in both this
        module's visitors and polyglot.py's PolyglotGraphBuilder. Keying by id
        lets the same relationship be re-emitted idempotently across passes
        without producing duplicate edges in the final AnalysisReport.
        """
        self.edges[edge.id] = edge
        log_event(
            logger,
            logging.DEBUG,
            "graph.edge_added",
            "Graph edge added",
            edge_id=edge.id,
            kind=edge.kind,
            source=edge.source,
            target=edge.target,
            evidence_status=edge.evidence.status,
            resolution=edge.evidence.resolution,
        )

    @traced("graph.add_unresolved")
    def add_unresolved(self, reference: UnresolvedReference) -> None:
        """Records an import/call/instantiation that couldn't be resolved, bounding how many *call* details are stored.

        Input: an `UnresolvedReference`. Always increments `unresolved_total`
        (used for accurate stats), but only appends to the `unresolved` list
        (and increments `stored_unresolved_calls`) when the reference isn't a
        "call" kind or the per-run `max_unresolved_call_details` cap hasn't been
        reached yet. Called by RelationshipVisitor._record_unresolved and
        PolyglotGraphBuilder's `_unresolved`/`_extract_declarative_relationships`.
        This bound keeps memory/response size sane for repositories with huge
        numbers of unresolved calls while `unresolved_total` still reports the
        true count in AnalysisStats.
        """
        self.unresolved_total += 1
        should_store = reference.reference_kind != "call" or (
            self.stored_unresolved_calls < self.max_unresolved_call_details
        )
        if should_store:
            self.unresolved.append(reference)
            if reference.reference_kind == "call":
                self.stored_unresolved_calls += 1
        log_event(
            logger,
            logging.DEBUG,
            "graph.reference_unresolved",
            "Reference could not be resolved without guessing",
            source=reference.source,
            reference=reference.reference,
            reference_kind=reference.reference_kind,
            span=reference.evidence.span,
            resolution=reference.evidence.resolution,
        )


class DefinitionVisitor(ast.NodeVisitor):
    """First-pass AST visitor that emits a GraphNode + CONTAINS edge for every class/function/method definition in one module.

    Instantiated once per parsed Python module by RepositoryAnalyzer.analyze and
    run before any RelationshipVisitor, so that by the time relationships
    (imports/calls) are resolved, every definition across the whole repository
    already has a GraphNode and can be looked up by qualified name.
    """

    def __init__(
        self,
        module: ParsedModule,
        graph: GraphAccumulator,
        repository_node_id: str,
    ) -> None:
        """Sets up the visitor with a one-element parent stack seeded by the module's own GraphNode, ready to descend into it."""
        self.module = module
        self.graph = graph
        self.parents: list[GraphNode] = [
            graph.nodes[module.module_node_id]
        ]
        self.qualifier_parts: list[str] = []
        self.repository_node_id = repository_node_id

    def _add_definition(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        kind: NodeKind,
        signature: str | None,
    ) -> GraphNode:
        """Creates a GraphNode for a class/function/method AST node and links it to its lexical parent with a CONTAINS edge.

        Input: the definition `node` (ClassDef/FunctionDef/AsyncFunctionDef),
        its `kind` (CLASS/FUNCTION/METHOD), and an optional `signature` string.
        Builds the qualified name from the module name plus current qualifier
        stack, derives a stable id from (kind, qualified_name, path, line), adds
        the node and a VERIFIED "AST lexical containment" edge from
        `self.parents[-1]`, and returns the new node. Called by `visit_ClassDef`
        and `_visit_function` for every definition encountered; the stable id
        formula is what keeps a definition's identity consistent across
        re-analysis runs of an unchanged file.
        """
        qualified_name = ".".join(
            [self.module.module_name, *self.qualifier_parts, node.name]
        )
        span = _span(self.module.relative_path, node)
        graph_node = GraphNode(
            id=_stable_id(kind, qualified_name, span.path, span.start_line),
            kind=kind,
            name=node.name,
            qualified_name=qualified_name,
            module=self.module.module_name,
            span=span,
            signature=signature,
            metadata={"language": "python"},
        )
        self.graph.add_node(graph_node)
        parent = self.parents[-1]
        evidence = Evidence(
            status=EvidenceStatus.VERIFIED,
            span=span,
            syntax=f"{kind.value} {node.name}",
            resolution="AST lexical containment",
            confidence=1.0,
        )
        self.graph.add_edge(
            GraphEdge(
                id=_stable_id(EdgeKind.CONTAINS, parent.id, graph_node.id),
                source=parent.id,
                target=graph_node.id,
                kind=EdgeKind.CONTAINS,
                evidence=evidence,
            )
        )
        return graph_node

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        """Records a class definition, then descends into its body with the class pushed as the new lexical parent/qualifier.

        Input: an `ast.ClassDef` node, supplied automatically by
        `ast.NodeVisitor.visit`/`generic_visit` while `RepositoryAnalyzer.analyze`
        walks a module's tree via `DefinitionVisitor(...).visit(tree)`. Pushing
        the class onto `self.parents`/`self.qualifier_parts` before recursing
        (and popping after) is what gives nested methods correct
        `Module.Class.method` qualified names and CONTAINS edges.
        """
        graph_node = self._add_definition(node, NodeKind.CLASS, None)
        self.parents.append(graph_node)
        self.qualifier_parts.append(node.name)
        self.generic_visit(node)
        self.qualifier_parts.pop()
        self.parents.pop()

    def _visit_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        """Shared logic for function/method definitions: classifies FUNCTION vs METHOD, records it, and recurses into its body.

        Input: an `ast.FunctionDef` or `ast.AsyncFunctionDef` node. Whether the
        definition is a METHOD or a top-level FUNCTION is decided purely by
        whether the current lexical parent (`self.parents[-1]`) is a CLASS
        node; the parameter list is unparsed into a `signature` string (falling
        back to `None` on failure). Called by `visit_FunctionDef` and
        `visit_AsyncFunctionDef` so sync and async definitions are treated
        identically for graph purposes.
        """
        parent_is_class = self.parents[-1].kind == NodeKind.CLASS
        kind = NodeKind.METHOD if parent_is_class else NodeKind.FUNCTION
        try:
            signature = ast.unparse(node.args)
        except Exception:
            signature = None
        graph_node = self._add_definition(node, kind, signature)
        self.parents.append(graph_node)
        self.qualifier_parts.append(node.name)
        self.generic_visit(node)
        self.qualifier_parts.pop()
        self.parents.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        """Dispatches a `def` statement to `_visit_function`; invoked automatically by `ast.NodeVisitor` during tree traversal."""
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        """Dispatches an `async def` statement to `_visit_function`; invoked automatically by `ast.NodeVisitor` during tree traversal."""
        self._visit_function(node)


class RelationshipVisitor(ast.NodeVisitor):
    """Second-pass AST visitor that resolves imports, calls, and instantiations for one module into edges or unresolved references.

    Run by RepositoryAnalyzer.analyze once per module, after every module in
    the repository has already been through DefinitionVisitor and the
    cross-module `symbols` index has been built, so this visitor can resolve
    references to definitions anywhere in the repository (not just the current
    file).
    """

    def __init__(
        self,
        module: ParsedModule,
        graph: GraphAccumulator,
        modules: dict[str, ParsedModule],
        symbols: dict[tuple[str, str], list[GraphNode]],
    ) -> None:
        """Initializes per-module state: the module being visited, the shared graph, all parsed modules, the symbol index, and traversal stacks."""
        self.module = module
        self.graph = graph
        self.modules = modules
        self.symbols = symbols
        self.scope_parts: list[str] = []
        self.source_stack: list[str] = [module.module_node_id]
        self.receiver_bindings: list[dict[str, GraphNode]] = [{}]

    def _current_package(self) -> list[str]:
        """Returns the dotted package-name parts that relative imports in this module should resolve against.

        No inputs beyond `self`. Treats the module as its own package if the
        underlying file is `__init__.py` (keeping all its module-name parts),
        otherwise drops the last (module) part to get the containing package.
        Called by `_from_base` while resolving `ast.ImportFrom` nodes, since
        Python's relative-import semantics differ for packages vs. plain
        modules.
        """
        module_parts = self.module.module_name.split(".")
        is_package = self.module.path.name == "__init__.py"
        return module_parts if is_package else module_parts[:-1]

    def _from_base(self, node: ast.ImportFrom) -> str:
        """Computes the fully-qualified base module string for a `from ... import ...` statement.

        Input: an `ast.ImportFrom` node. Combines `_current_package()` with the
        node's relative-import `level` (number of leading dots, climbing up the
        package hierarchy accordingly) and its dotted `module` name, if any.
        Called by `visit_ImportFrom` to build the candidate module name(s) to
        check against `self.modules` before falling back to an unresolved
        reference.
        """
        package = self._current_package()
        if node.level:
            climb = max(0, node.level - 1)
            package = package[: max(0, len(package) - climb)]
        elif node.module:
            package = []
        module_parts = node.module.split(".") if node.module else []
        return ".".join([*package, *module_parts])

    def _import_edge(
        self, node: ast.AST, target_module: str, resolution: str
    ) -> None:
        """Emits a VERIFIED IMPORTS edge from the current module to an already-confirmed internal `target_module`.

        Input: the originating AST `node` (for span/syntax), the `target_module`
        name (must already be a key in `self.modules`), and a `resolution`
        explanation string. Called by `visit_Import` and `visit_ImportFrom` only
        after they've confirmed the imported module is part of this
        repository's indexed modules.
        """
        target = self.modules[target_module]
        span = _span(self.module.relative_path, node)
        self.graph.add_edge(
            GraphEdge(
                id=_stable_id(
                    EdgeKind.IMPORTS,
                    self.module.module_node_id,
                    target.module_node_id,
                    span.start_line,
                    _syntax(node),
                ),
                source=self.module.module_node_id,
                target=target.module_node_id,
                kind=EdgeKind.IMPORTS,
                evidence=Evidence(
                    status=EvidenceStatus.VERIFIED,
                    span=span,
                    syntax=_syntax(node),
                    resolution=resolution,
                    confidence=1.0,
                ),
            )
        )

    def visit_Import(self, node: ast.Import) -> Any:
        """Handles plain `import x[.y][ as z]` statements: records internal alias mappings and IMPORTS edges, or files unresolved references.

        Input: an `ast.Import` node, dispatched automatically while walking a
        module's tree (`RelationshipVisitor(...).visit(tree)` in
        `RepositoryAnalyzer.analyze`). For each imported name that matches an
        internal module, records the local alias in
        `self.module.imported_modules` and emits an IMPORTS edge; otherwise
        files an unresolved "import" reference flagged with whether its root
        package looks external (not among any internal module's top-level
        name).
        """
        internal_roots = {name.split(".", 1)[0] for name in self.modules}
        for alias in node.names:
            if alias.name in self.modules:
                local_name = alias.asname or alias.name.split(".")[0]
                self.module.imported_modules[local_name] = alias.name
                self._import_edge(node, alias.name, "Exact internal module import")
            else:
                self._record_unresolved(
                    node,
                    alias.name,
                    "import",
                    "Import does not match an indexed internal module",
                    metadata={
                        "external": alias.name.split(".", 1)[0] not in internal_roots,
                        "package": alias.name.split(".", 1)[0],
                    },
                )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        """Handles `from x import y [as z]` statements, trying an internal-submodule match before an internal-symbol match.

        Input: an `ast.ImportFrom` node, dispatched automatically during tree
        traversal. First checks whether `base.name` (base from `_from_base`) is
        itself an internal module (a submodule import); if not, checks whether
        `base` alone is internal and records `y` as an imported symbol from it;
        otherwise files an unresolved "import" reference with an
        external/package hint. This two-step resolution mirrors how Python
        itself must distinguish `from pkg import submodule` from
        `from pkg import some_function`.
        """
        base = self._from_base(node)
        internal_roots = {name.split(".", 1)[0] for name in self.modules}
        for alias in node.names:
            candidate_module = ".".join(filter(None, [base, alias.name]))
            local_name = alias.asname or alias.name
            if candidate_module in self.modules:
                self.module.imported_modules[local_name] = candidate_module
                self._import_edge(
                    node,
                    candidate_module,
                    "Resolved imported name as internal submodule",
                )
            elif base in self.modules:
                self._import_edge(
                    node,
                    base,
                    "Resolved from-import base as internal module",
                )
                self.module.imported_symbols[local_name] = (base, alias.name)
            else:
                self._record_unresolved(
                    node,
                    _syntax(node),
                    "import",
                    "From-import base and candidate submodule are not internal",
                    metadata={
                        "external": bool(base)
                        and node.level == 0
                        and base.split(".", 1)[0] not in internal_roots,
                        "package": base.split(".", 1)[0] if base else alias.name,
                    },
                )

    def _definition_node(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    ) -> GraphNode | None:
        """Looks up the GraphNode that DefinitionVisitor already created for this class/function AST node.

        Input: the definition node currently being visited. Rebuilds the same
        qualified name DefinitionVisitor would have used, looks up candidates in
        `self.symbols`, and disambiguates by matching the candidate's span
        start line to the node's `lineno` (falling back to the first candidate
        if no exact line match is found). Called by `visit_ClassDef` and
        `_visit_function` to know which GraphNode id to push onto
        `source_stack`/`scope_parts` as the "current" definition while
        descending into it.
        """
        qualified = ".".join(
            [self.module.module_name, *self.scope_parts, node.name]
        )
        candidates = self.symbols.get((self.module.module_name, qualified), [])
        if not candidates:
            return None
        return next(
            (
                candidate
                for candidate in candidates
                if candidate.span
                and candidate.span.start_line == getattr(node, "lineno", -1)
            ),
            candidates[0],
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        """Enters a class's scope for relationship resolution, pushing its name/GraphNode id before recursing into its body.

        Input: an `ast.ClassDef` node, dispatched automatically during tree
        traversal. Pushing the class's id onto `source_stack` lets calls made
        directly in the class body (e.g. class-level assignments) attribute to
        the class; `scope_parts` tracks nesting for qualified-name lookups by
        `_definition_node`. Both stacks are popped again after `generic_visit`
        returns.
        """
        graph_node = self._definition_node(node)
        self.scope_parts.append(node.name)
        if graph_node:
            self.source_stack.append(graph_node.id)
        self.generic_visit(node)
        if graph_node:
            self.source_stack.pop()
        self.scope_parts.pop()

    def _visit_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        """Enters a function/method scope, records class-typed argument annotations as receiver bindings, then recurses.

        Input: an `ast.FunctionDef`/`ast.AsyncFunctionDef` node. Pushes the
        function's GraphNode id and a fresh (empty) receiver-bindings frame,
        then scans positional/keyword-only args for `Name` annotations that
        resolve to an internal class (e.g. `def f(x: Foo)`), recording `x ->
        Foo` in `self.receiver_bindings[-1]` so calls like `x.method()` inside
        the body can later be resolved via `_resolve_call`. Called by
        `visit_FunctionDef`/`visit_AsyncFunctionDef`; all pushed state is popped
        again after `generic_visit` returns.
        """
        graph_node = self._definition_node(node)
        self.scope_parts.append(node.name)
        if graph_node:
            self.source_stack.append(graph_node.id)
        self.receiver_bindings.append({})
        for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            if isinstance(argument.annotation, ast.Name):
                target, _ = self._resolve_named_symbol(argument.annotation.id)
                if target is not None and target.kind == NodeKind.CLASS:
                    self.receiver_bindings[-1][argument.arg] = target
        self.generic_visit(node)
        self.receiver_bindings.pop()
        if graph_node:
            self.source_stack.pop()
        self.scope_parts.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        """Dispatches a `def` statement to `_visit_function`; invoked automatically by `ast.NodeVisitor` during tree traversal."""
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        """Dispatches an `async def` statement to `_visit_function`; invoked automatically by `ast.NodeVisitor` during tree traversal."""
        self._visit_function(node)

    def _resolve_named_symbol(self, name: str) -> tuple[GraphNode | None, str]:
        """Resolves a bare identifier (e.g. `foo` in `foo(...)`) to a single unique GraphNode, without guessing among ambiguous matches.

        Input: the identifier string `name`. Checks imported-symbol aliases
        first (only if that resolves to exactly one candidate), then
        same-module symbols by that name (again only if unique). Called by
        `_resolve_call`, `visit_Assign`, and `visit_AnnAssign`. Returning
        `(None, reason)` instead of picking an arbitrary match when a name is
        ambiguous or unknown is what keeps emitted call/construction edges
        conservative and high-confidence.
        """
        imported = self.module.imported_symbols.get(name)
        if imported:
            candidates = self.symbols.get(imported, [])
            if len(candidates) == 1:
                return candidates[0], "Resolved imported symbol alias"
        candidates = self.symbols.get((self.module.module_name, name), [])
        if len(candidates) == 1:
            return candidates[0], "Resolved unique same-module symbol"
        return None, "Bare call target is not uniquely resolvable"

    def _unique_imported_member(self, name: str) -> GraphNode | None:
        """Conservatively recover an unknown receiver from internal imports.

        This remains an inference: it is accepted only when exactly one function or
        method with this name exists across the source module's internal imports.

        Input: an attribute name (e.g. `attr` from `obj.attr(...)`). Called by
        `_resolve_call` as a last-resort fallback when the receiver's type
        cannot be determined by any other means (annotation, `self`/`cls`,
        imported-module attribute). Exists to still recover call edges for
        dynamically-typed receivers without producing a false-positive edge when
        multiple same-named methods exist across the module's imports.
        """
        module_names = set(self.module.imported_modules.values()) | {
            module_name
            for module_name, _symbol_name in self.module.imported_symbols.values()
        }
        candidates = {
            candidate.id: candidate
            for module_name in module_names
            for candidate in self.symbols.get((module_name, name), [])
            if candidate.kind in {NodeKind.FUNCTION, NodeKind.METHOD}
        }
        return next(iter(candidates.values())) if len(candidates) == 1 else None

    def _resolve_call(
        self, node: ast.Call
    ) -> tuple[GraphNode | None, str, float]:
        """Attempts to resolve an `ast.Call` node's callee to a specific GraphNode, trying several strategies in order of confidence.

        Input: an `ast.Call` node. Tries, in order: a bare name via
        `_resolve_named_symbol` (0.8 confidence); `module.attr` where `module`
        is a known imported module (0.8); `self.attr`/`cls.attr` resolved
        against the lexically enclosing class (0.8); `x.attr` where `x` was
        bound to a class via `visit_Assign`/`visit_AnnAssign`/annotated
        parameter (0.8); and finally `_unique_imported_member` as a conservative
        fallback (0.55, or 0.5 for fully dynamic attribute chains). Called by
        `visit_Call`. This is the core heuristic engine behind the Python
        call-graph inference and its confidence scores.
        """
        function = node.func
        if isinstance(function, ast.Name):
            target, resolution = self._resolve_named_symbol(function.id)
            return target, resolution, 0.8
        if isinstance(function, ast.Attribute) and isinstance(
            function.value, ast.Name
        ):
            base = function.value.id
            if base in self.module.imported_modules:
                target_module = self.module.imported_modules[base]
                candidates = self.symbols.get(
                    (target_module, function.attr), []
                )
                if len(candidates) == 1:
                    return candidates[0], "Resolved attribute on imported module", 0.8
            if base in {"self", "cls"}:
                class_parts = self.scope_parts[:-1]
                if class_parts:
                    qualified = ".".join(
                        [self.module.module_name, *class_parts, function.attr]
                    )
                    candidates = self.symbols.get(
                        (self.module.module_name, qualified), []
                    )
                    if len(candidates) == 1:
                        return candidates[0], "Resolved method on lexical class", 0.8
            bound_type = self.receiver_bindings[-1].get(base)
            if bound_type is not None:
                qualified = f"{bound_type.qualified_name}.{function.attr}"
                candidates = self.symbols.get(
                    (bound_type.module or self.module.module_name, qualified), []
                )
                if len(candidates) == 1:
                    return (
                        candidates[0],
                        "Resolved method through locally constructed or annotated receiver",
                        0.8,
                    )
            recovered = self._unique_imported_member(function.attr)
            if recovered is not None:
                return (
                    recovered,
                    "Conservatively inferred unique matching member across internal imports",
                    0.55,
                )
            return None, "Attribute receiver type is not statically established", 0.0
        if isinstance(function, ast.Attribute):
            recovered = self._unique_imported_member(function.attr)
            if recovered is not None:
                return (
                    recovered,
                    "Conservatively inferred dynamic receiver from unique internal imported member",
                    0.5,
                )
        return None, "Dynamic call expression is not statically resolvable", 0.0

    def visit_Assign(self, node: ast.Assign) -> Any:
        """Tracks simple `x = Foo(...)` assignments so later `x.method()` calls can resolve against class `Foo`.

        Input: an `ast.Assign` node, dispatched automatically during traversal.
        Only handles the direct pattern of a `Call` to a bare `Name` that
        resolves (via `_resolve_named_symbol`) to a class, recording the bound
        type for every simple `Name` target in `self.receiver_bindings[-1]`.
        Feeds `_resolve_call`'s "locally constructed or annotated receiver"
        branch.
        """
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
            constructed, _ = self._resolve_named_symbol(node.value.func.id)
            if constructed is not None and constructed.kind == NodeKind.CLASS:
                for assignment_target in node.targets:
                    if isinstance(assignment_target, ast.Name):
                        self.receiver_bindings[-1][assignment_target.id] = constructed
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:
        """Tracks annotated assignments like `x: Foo` (with or without a value) as receiver-type bindings, mirroring `visit_Assign`.

        Input: an `ast.AnnAssign` node, dispatched automatically during
        traversal. If the target is a bare `Name` and the annotation is a bare
        `Name` that resolves to an internal class, records that binding so
        later `x.method()` calls in the same scope can be resolved.
        """
        if isinstance(node.target, ast.Name) and isinstance(node.annotation, ast.Name):
            declared, _ = self._resolve_named_symbol(node.annotation.id)
            if declared is not None and declared.kind == NodeKind.CLASS:
                self.receiver_bindings[-1][node.target.id] = declared
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        """Resolves a call/construction expression and records either a MAY_CALL/INSTANTIATES edge or an unresolved "call" reference.

        Input: an `ast.Call` node, dispatched automatically during traversal.
        Delegates to `_resolve_call` for the target/resolution/confidence; if a
        target was found, emits an INSTANTIATES edge (when the target is a
        class) or a MAY_CALL edge (otherwise) with INFERRED evidence at the
        given confidence, sourced from `self.source_stack[-1]` (the innermost
        enclosing definition); otherwise files an unresolved "call" reference
        via `_record_unresolved`. This is the single call site that turns
        Python call expressions into graph edges.
        """
        target, resolution, confidence = self._resolve_call(node)
        source = self.source_stack[-1]
        span = _span(self.module.relative_path, node)
        if target:
            relationship = (
                EdgeKind.INSTANTIATES
                if target.kind == NodeKind.CLASS
                else EdgeKind.MAY_CALL
            )
            self.graph.add_edge(
                GraphEdge(
                    id=_stable_id(
                        relationship,
                        source,
                        target.id,
                        span.start_line,
                        span.start_column,
                    ),
                    source=source,
                    target=target.id,
                    kind=relationship,
                    evidence=Evidence(
                        status=EvidenceStatus.INFERRED,
                        span=span,
                        syntax=_syntax(node),
                        resolution=(
                            f"{resolution}; resolved class construction"
                            if relationship == EdgeKind.INSTANTIATES
                            else resolution
                        ),
                        confidence=confidence,
                    ),
                    metadata={
                        "call_site_kind": (
                            "instantiation"
                            if relationship == EdgeKind.INSTANTIATES
                            else "call"
                        )
                    },
                )
            )
        else:
            self._record_unresolved(
                node,
                _syntax(node.func),
                "call",
                resolution,
                source=source,
            )
        self.generic_visit(node)

    def _record_unresolved(
        self,
        node: ast.AST,
        reference: str,
        reference_kind: str,
        resolution: str,
        source: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Builds and stores an UnresolvedReference with UNRESOLVED evidence for an import/call that couldn't be matched internally.

        Input: the originating `node` (for span/syntax), a human-readable
        `reference` string, its `reference_kind` ("import" or "call"), a
        `resolution` explanation, an optional `source` node id override
        (defaults to the current module), and optional `metadata` (e.g.
        `external`/`package` flags used to distinguish third-party dependencies
        from genuinely unresolvable internal references). Called by
        `visit_Import`, `visit_ImportFrom`, and `visit_Call`. Feeds
        `AnalysisStats.unresolved_count` and `AnalysisReport.unresolved_references`,
        which the UI/agent use to surface external dependencies and analysis
        gaps.
        """
        self.graph.add_unresolved(
            UnresolvedReference(
                source=source or self.module.module_node_id,
                reference=reference,
                reference_kind=reference_kind,
                evidence=Evidence(
                    status=EvidenceStatus.UNRESOLVED,
                    span=_span(self.module.relative_path, node),
                    syntax=_syntax(node),
                    resolution=resolution,
                    confidence=0.0,
                ),
                metadata=metadata or {},
            )
        )


class RepositoryAnalyzer:
    """Top-level orchestrator that scans a repository and produces a full AnalysisReport of its code graph.

    Discovers source files, parses every Python file with `ast` (via
    DefinitionVisitor then RelationshipVisitor) and hands non-Python files to
    PolyglotGraphBuilder, then assembles nodes/edges/diagnostics/stats into an
    AnalysisReport. This is the main pipeline entry point: called by
    backend/app/api/routes.py:_analyze on every `/analysis` request,
    backend/app/mcp_server.py's index/analyze handlers, and
    backend/app/agent/retrieval.py's RepositoryIndexCache.get/.rebuild whenever a
    repository needs to be (re)scanned, as well as throughout backend/tests/.
    """

    def __init__(
        self,
        *,
        max_files: int | None = None,
        max_file_bytes: int | None = None,
        max_unresolved_call_details: int | None = None,
    ) -> None:
        """Reads file-count/file-size/unresolved-detail limits from backend.app.config.settings unless explicit overrides are passed."""
        self.max_files = max_files or settings.max_repository_files
        self.max_file_bytes = max_file_bytes or settings.max_python_file_bytes
        self.max_unresolved_call_details = (
            settings.max_unresolved_call_details
            if max_unresolved_call_details is None
            else max_unresolved_call_details
        )

    @traced("parser.discover_files")
    def discover_source_files(self, root: Path) -> list[Path]:
        """Walks the repository tree and returns the sorted list of supported source file paths, enforcing `max_files`.

        Input: an absolute repository root `Path`. Skips VCS/build/venv
        directories (`_SKIPPED_DIRECTORIES`) and symlinked directories/files,
        rejects any path that resolves outside the repository root or overlaps
        the configured `settings.clone_root` (a defense against symlink/clone
        escape), and keeps only files whose suffix is `.py` or in
        `SUPPORTED_EXTENSIONS` (the polyglot languages). Raises `ValueError` if
        more than `self.max_files` files are found. Called by `analyze()` (and,
        filtered to `.py`, by `discover_python_files`) to build a deterministic,
        security-conscious file inventory before any parsing begins.
        """
        discovered: list[Path] = []
        resolved_root = root.resolve()
        clone_root = settings.clone_root.resolve()
        supported = {".py", *SUPPORTED_EXTENSIONS}
        for current_root, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current_root)
            retained_directories: list[str] = []
            for directory in sorted(directories):
                candidate_directory = current_path / directory
                if directory in _SKIPPED_DIRECTORIES or candidate_directory.is_symlink():
                    continue
                try:
                    resolved_directory = candidate_directory.resolve()
                except OSError:
                    continue
                if (
                    resolved_root != clone_root
                    and clone_root.is_relative_to(resolved_root)
                    and (
                        resolved_directory == clone_root
                        or resolved_directory.is_relative_to(clone_root)
                    )
                ):
                    # Avoid recursing into this tool's own clone-storage directory when it
                    # happens to live inside the repository being analyzed.
                    continue
                retained_directories.append(directory)
            directories[:] = retained_directories
            for filename in sorted(filenames):
                path = current_path / filename
                if path.suffix.lower() not in supported:
                    continue
                relative = path.relative_to(root)
                try:
                    resolved_path = path.resolve()
                    resolved_path.relative_to(resolved_root)
                except (OSError, ValueError):
                    log_event(
                        logger,
                        logging.WARNING,
                        "security.source_path_rejected",
                        "Source path skipped because it resolves outside the repository",
                        path=relative,
                    )
                    continue
                if path.is_symlink() or not resolved_path.is_file():
                    log_event(
                        logger,
                        logging.WARNING,
                        "security.source_path_rejected",
                        "Source path skipped because symbolic and non-file entries are not analyzed",
                        path=relative,
                    )
                    continue
                discovered.append(path)
                if len(discovered) > self.max_files:
                    raise ValueError(
                        f"Repository exceeds the {self.max_files} supported source file limit"
                    )
        discovered.sort(key=lambda item: item.as_posix())
        log_event(
            logger,
            logging.INFO,
            "parser.discovery_completed",
            "Multi-language repository discovery completed",
            root=root,
            file_count=len(discovered),
            extensions=sorted({path.suffix.lower() for path in discovered}),
        )
        return discovered

    def discover_python_files(self, root: Path) -> list[Path]:
        """Compatibility helper retained for callers that explicitly want Python.

        Input: an absolute repository root `Path`. Filters
        `discover_source_files(root)` down to `.py` paths only. Called directly
        by backend/tests/test_analyzer.py to assert Python-only discovery
        behavior; `analyze()` itself now calls `discover_source_files` and
        handles polyglot languages separately, so this method exists purely for
        backward compatibility with callers that only care about Python.
        """
        return [
            path
            for path in self.discover_source_files(root)
            if path.suffix.lower() == ".py"
        ]

    @traced("parser.parse_file")
    def _parse_file(
        self,
        root: Path,
        path: Path,
        graph: GraphAccumulator,
        import_roots: tuple[Path, ...],
    ) -> ParsedModule | None:
        """Parses one Python file with `ast`, records diagnostics on size/parse failures, and creates its MODULE GraphNode.

        Input: the repository `root`, the absolute file `path`, the shared
        `graph` accumulator, and the `import_roots` tuple from `_source_roots`.
        Returns `None` (after appending a warning/error `AnalysisDiagnostic`
        instead of raising) if the file exceeds `self.max_file_bytes` or fails
        to read/parse; otherwise builds the module's `GraphNode` — including a
        `content_sha256` of its source, used elsewhere to detect unchanged files
        — and returns a `ParsedModule` wrapping the AST. Called once per
        discovered `.py` file by `analyze()`, before `DefinitionVisitor` runs on
        the same tree. Returning `None` instead of raising is what lets one bad
        file be skipped without aborting the whole repository scan.
        """
        relative = path.relative_to(root)
        relative_string = relative.as_posix()
        size = path.stat().st_size
        log_event(
            logger,
            logging.DEBUG,
            "parser.file_started",
            "Parsing Python file",
            path=relative_string,
            size_bytes=size,
        )
        if size > self.max_file_bytes:
            graph.diagnostics.append(
                AnalysisDiagnostic(
                    severity="warning",
                    code="file_too_large",
                    message=(
                        f"File is {size} bytes; limit is {self.max_file_bytes}"
                    ),
                    path=relative_string,
                )
            )
            log_event(
                logger,
                logging.WARNING,
                "parser.file_skipped",
                "Python file exceeds configured byte limit",
                path=relative_string,
                size_bytes=size,
                limit_bytes=self.max_file_bytes,
            )
            return None
        try:
            with tokenize.open(path) as source_file:
                source = source_file.read()
            tree = ast.parse(source, filename=relative_string)
        except (OSError, UnicodeError, SyntaxError) as exc:
            graph.diagnostics.append(
                AnalysisDiagnostic(
                    severity="error",
                    code="parse_failed",
                    message=f"{type(exc).__name__}: {exc}",
                    path=relative_string,
                    line=getattr(exc, "lineno", None),
                )
            )
            log_event(
                logger,
                logging.ERROR,
                "parser.file_failed",
                "Python file could not be parsed",
                path=relative_string,
                exception_type=type(exc).__name__,
                exception_message=str(exc),
                line=getattr(exc, "lineno", None),
            )
            return None
        module_relative = _module_relative_path(root, path, import_roots)
        module_name = _module_name(module_relative)
        module_span = SourceSpan(
            path=relative_string,
            start_line=1,
            start_column=0,
            end_line=max(1, len(source.splitlines())),
            end_column=0,
        )
        module_node = GraphNode(
            id=_stable_id(NodeKind.MODULE, module_name, relative_string),
            kind=NodeKind.MODULE,
            name=relative.stem,
            qualified_name=module_name,
            module=module_name,
            span=module_span,
            metadata={
                "language": "python",
                "size_bytes": size,
                "content_sha256": hashlib.sha256(
                    source.encode("utf-8")
                ).hexdigest(),
            },
        )
        graph.add_node(module_node)
        log_event(
            logger,
            logging.INFO,
            "parser.file_completed",
            "Python file parsed successfully",
            path=relative_string,
            module=module_name,
            ast_statement_count=len(tree.body),
        )
        return ParsedModule(
            path=path,
            relative_path=relative_string,
            module_name=module_name,
            tree=tree,
            module_node_id=module_node.id,
        )

    @traced("parser.analyze_repository")
    def analyze(self, repository_root: Path) -> AnalysisReport:
        """Runs the full repository analysis pipeline and returns the resulting AnalysisReport.

        Input: `repository_root`, any `Path` to an existing directory (expanded
        and resolved internally); raises `FileNotFoundError`/`NotADirectoryError`
        if it isn't a valid directory. Pipeline: discover files -> parse each
        `.py` file into a MODULE node and run `DefinitionVisitor` over it ->
        hand non-Python files to `PolyglotGraphBuilder` -> build a cross-module
        `symbols` index from every definition -> re-parse each Python module and
        run `RelationshipVisitor` (imports/calls/instantiations) -> resolve
        polyglot relationships -> assemble `AnalysisStats`/diagnostics into an
        `AnalysisReport`. Called by backend/app/api/routes.py:_analyze,
        backend/app/mcp_server.py, backend/app/agent/retrieval.py's
        RepositoryIndexCache.get/.rebuild, and essentially every analyzer/
        polyglot/agent test in backend/tests/ — it is the sole entry point that
        turns a repository on disk into the graph the rest of the app queries.
        The two-pass design (all definitions indexed before any relationships
        are resolved) is what lets calls/imports correctly resolve to
        definitions in other files, including ones parsed later in the walk.
        """
        started = time.perf_counter()
        root = repository_root.expanduser().resolve()
        if not root.exists():
            raise FileNotFoundError(f"Repository path does not exist: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"Repository path is not a directory: {root}")
        log_event(
            logger,
            logging.INFO,
            "parser.analysis_started",
            "Repository analysis started",
            repository_root=root,
            max_files=self.max_files,
            max_file_bytes=self.max_file_bytes,
        )
        graph = GraphAccumulator(self.max_unresolved_call_details)
        import_roots = _source_roots(root)
        log_event(
            logger,
            logging.INFO,
            "parser.source_roots_detected",
            "Language source roots selected for module-name resolution",
            repository_root=root,
            import_roots=import_roots,
        )
        repository_node = GraphNode(
            id=_stable_id(NodeKind.REPOSITORY, root.name, root),
            kind=NodeKind.REPOSITORY,
            name=root.name,
            qualified_name=root.name,
            metadata={"root": str(root)},
        )
        graph.add_node(repository_node)
        files = self.discover_source_files(root)
        python_files = [path for path in files if path.suffix.lower() == ".py"]
        modules: dict[str, ParsedModule] = {}
        skipped = 0
        for path in python_files:
            parsed = self._parse_file(root, path, graph, import_roots)
            if parsed is None:
                skipped += 1
                continue
            if parsed.module_name in modules:
                existing = modules[parsed.module_name]
                graph.diagnostics.append(
                    AnalysisDiagnostic(
                        severity="warning",
                        code="duplicate_module_name",
                        message=(
                            f"Module name {parsed.module_name!r} also maps to "
                            f"{existing.relative_path}; keeping the first file"
                        ),
                        path=parsed.relative_path,
                    )
                )
                log_event(
                    logger,
                    logging.WARNING,
                    "parser.duplicate_module_name",
                    "Multiple files resolve to the same Python module name",
                    module=parsed.module_name,
                    existing_path=existing.relative_path,
                    duplicate_path=parsed.relative_path,
                )
                graph.nodes.pop(parsed.module_node_id, None)
                skipped += 1
                continue
            modules[parsed.module_name] = parsed
            module_node = graph.nodes[parsed.module_node_id]
            graph.add_edge(
                GraphEdge(
                    id=_stable_id(
                        EdgeKind.CONTAINS, repository_node.id, module_node.id
                    ),
                    source=repository_node.id,
                    target=module_node.id,
                    kind=EdgeKind.CONTAINS,
                    evidence=Evidence(
                        status=EvidenceStatus.VERIFIED,
                        span=module_node.span,
                        syntax=parsed.relative_path,
                        resolution="File discovered beneath repository root",
                        confidence=1.0,
                    ),
                )
            )
            if parsed.tree is not None:
                DefinitionVisitor(parsed, graph, repository_node.id).visit(
                    parsed.tree
                )
                parsed.tree = None

        polyglot = PolyglotGraphBuilder(root, graph, self.max_file_bytes)
        for path in files:
            if path.suffix.lower() == ".py":
                continue
            parsed = polyglot.parse_file(path)
            if parsed is None:
                skipped += 1
                continue
            polyglot.attach_module(parsed, repository_node)

        symbols: dict[tuple[str, str], list[GraphNode]] = {}
        for node in graph.nodes.values():
            if node.module and node.kind in {
                NodeKind.CLASS,
                NodeKind.FUNCTION,
                NodeKind.METHOD,
            }:
                symbols.setdefault((node.module, node.name), []).append(node)
                symbols.setdefault(
                    (node.module, node.qualified_name), []
                ).append(node)

        for module in modules.values():
            try:
                with tokenize.open(module.path) as source_file:
                    relationship_source = source_file.read()
                relationship_tree = ast.parse(
                    relationship_source, filename=module.relative_path
                )
            except (OSError, UnicodeError, SyntaxError) as exc:
                graph.diagnostics.append(
                    AnalysisDiagnostic(
                        severity="error",
                        code="relationship_reparse_failed",
                        message=f"{type(exc).__name__}: {exc}",
                        path=module.relative_path,
                        line=getattr(exc, "lineno", None),
                    )
                )
                log_event(
                    logger,
                    logging.ERROR,
                    "parser.relationship_reparse_failed",
                    "Previously parsed file failed during relationship pass",
                    path=module.relative_path,
                    exception_type=type(exc).__name__,
                    exception_message=str(exc),
                )
                continue
            RelationshipVisitor(module, graph, modules, symbols).visit(
                relationship_tree
            )
        polyglot.resolve_relationships()
        omitted_unresolved_calls = max(
            0,
            graph.unresolved_total - len(graph.unresolved),
        )
        if omitted_unresolved_calls:
            graph.diagnostics.append(
                AnalysisDiagnostic(
                    severity="info",
                    code="unresolved_call_details_truncated",
                    message=(
                        f"Omitted {omitted_unresolved_calls} unresolved call "
                        "details after reaching the configured storage limit; "
                        "the total remains available in analysis statistics"
                    ),
                )
            )
            log_event(
                logger,
                logging.WARNING,
                "graph.unresolved_details_truncated",
                "Unresolved call details were bounded for repository scale",
                total_unresolved=graph.unresolved_total,
                stored_details=len(graph.unresolved),
                omitted_calls=omitted_unresolved_calls,
                call_detail_limit=self.max_unresolved_call_details,
            )

        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        parse_failures = sum(
            diagnostic.code == "parse_failed" for diagnostic in graph.diagnostics
        )
        report = AnalysisReport(
            repository_root=str(root),
            repository_name=root.name,
            nodes=sorted(graph.nodes.values(), key=lambda node: node.id),
            edges=sorted(graph.edges.values(), key=lambda edge: edge.id),
            unresolved_references=graph.unresolved,
            diagnostics=graph.diagnostics,
            stats=AnalysisStats(
                files_discovered=len(files),
                files_parsed=len(modules) + len(polyglot.modules),
                files_skipped=skipped,
                parse_failures=parse_failures,
                node_count=len(graph.nodes),
                edge_count=len(graph.edges),
                unresolved_count=graph.unresolved_total,
                duration_ms=duration_ms,
            ),
        )
        log_event(
            logger,
            logging.INFO,
            "parser.analysis_completed",
            "Repository analysis completed",
            repository_root=root,
            stats=report.stats,
            diagnostic_count=len(report.diagnostics),
        )
        return report
