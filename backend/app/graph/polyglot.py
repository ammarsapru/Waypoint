from __future__ import annotations

import hashlib
import logging
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from tree_sitter import Language, Node, Parser
import tree_sitter_java
import tree_sitter_javascript
import tree_sitter_typescript
import tree_sitter_html
import tree_sitter_css

from backend.app.graph.models import (
    AnalysisDiagnostic,
    EdgeKind,
    Evidence,
    EvidenceStatus,
    GraphEdge,
    GraphNode,
    NodeKind,
    SourceSpan,
    UnresolvedReference,
)
from backend.app.observability import log_event, traced

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".java",
    ".html",
    ".htm",
    ".css",
}

_JS_EXTENSIONS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
_CLASS_TYPES = {
    "class",
    "class_declaration",
    "abstract_class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
    "annotation_type_declaration",
}
_JS_FUNCTION_TYPES = {"function_declaration", "generator_function_declaration"}
_JS_METHOD_TYPES = {"method_definition", "method_signature", "abstract_method_signature"}
_JAVA_METHOD_TYPES = {"method_declaration", "constructor_declaration"}


def _stable_id(*parts: object) -> str:
    """Derives a short, deterministic 24-char hex id from any number of values.

    Input: any positional args (str, int, etc.); each is stringified and joined
    with the ASCII unit-separator (\\x1f) to avoid delimiter collisions, then
    SHA-256 hashed. Identical to analyzer.py's `_stable_id` (duplicated to keep
    the modules independent); used throughout this file to build stable
    GraphNode/GraphEdge ids for JS/TS/Java/HTML/CSS definitions and
    relationships so re-analyzing an unchanged file reproduces the same ids.
    """
    material = "\x1f".join(str(part) for part in parts)  # join with a delimiter unlikely to appear in real values
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _text(source: bytes, node: Node | None, limit: int = 2_000) -> str:
    """Decodes the UTF-8 source text spanned by a tree-sitter node, truncated to `limit` characters.

    Input: the full file `source` bytes and a tree-sitter `Node` (or `None`,
    which yields `""`); decoding uses `errors="replace"` so malformed byte
    sequences don't raise. Called throughout this module (definition/import/
    call extraction) anywhere a node's literal text is needed, e.g. for names,
    signatures, or Evidence.syntax snippets.
    """
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")[:limit]


def _span(path: str, node: Node) -> SourceSpan:
    """Builds a 1-indexed SourceSpan from a tree-sitter node's `start_point`/`end_point`.

    Input: the file's `path` string and a tree-sitter `Node` (whose points are
    0-indexed rows). Called throughout definition/relationship extraction in
    this module wherever a GraphNode or Evidence needs a source location;
    mirrors analyzer.py's AST-based `_span` but reads tree-sitter's row/column
    tuples instead of AST attributes.
    """
    start_row, start_column = node.start_point
    end_row, end_column = node.end_point
    return SourceSpan(
        path=path,
        start_line=start_row + 1,
        start_column=start_column,
        end_line=max(start_row + 1, end_row + 1),
        end_column=end_column,
    )


def _node_key(node: Node) -> tuple[int, int, str]:
    """Builds a hashable identity key for a tree-sitter node from its byte range and type.

    Input: a tree-sitter `Node`. Tree-sitter nodes aren't directly hashable/
    reusable as dict keys across traversals, so this (start_byte, end_byte,
    type) triple stands in for node identity. Used as the key type for
    `PolyglotModule.definition_nodes` and the `require_calls` set in
    `_extract_relationships`, letting later passes look up "was this exact
    node already recorded as a definition/require-call".
    """
    return (node.start_byte, node.end_byte, node.type)


def _descendants(node: Node, types: set[str]) -> Iterable[Node]:
    """Recursively yields every named descendant of `node` (including `node` itself) whose tree-sitter type is in `types`.

    Input: a tree-sitter `Node` to search under and a set of type-name strings
    to match. Called throughout this module (e.g. to find `import_declaration`,
    `import_specifier`, `namespace_import`, `string` nodes) as a lightweight
    substitute for tree-sitter query objects; walks only `named_children` so
    punctuation/anonymous nodes are skipped.
    """
    if node.type in types:
        yield node
    for child in node.named_children:
        yield from _descendants(child, types)


def _strip_source_extension(value: str) -> str:
    """Removes a known source-file extension (longest match first) from a path-like string.

    Input: any string, typically a relative path or module specifier. Checks
    suffixes longest-first (`.d.ts` before `.ts`, etc.) so compound extensions
    aren't partially stripped. Called by `_path_key` and `_resolve_js_module`
    to normalize both indexed file paths and import specifiers to the same
    extension-less form before comparing them.
    """
    lowered = value.lower()
    for suffix in (".d.ts", ".tsx", ".mts", ".cts", ".jsx", ".mjs", ".cjs", ".html", ".htm", ".css", ".ts", ".js"):
        if lowered.endswith(suffix):
            return value[: -len(suffix)]
    return value


def _path_key(relative_path: str) -> str:
    """Normalizes a relative file path into the canonical key used to index/look up modules by location.

    Input: a relative path string (may use `\\` or `/` separators). Strips the
    source extension, collapses a trailing `/index` (so `foo/index.ts` and
    `foo` resolve to the same key), and trims slashes, defaulting to
    `"__root__"` for an empty result. Called by `parse_file` (to register a
    module) and by `_resolve_asset_module`/`_resolve_js_module` (to look one
    up), so both sides of a lookup agree on the same normalized key.
    """
    value = _strip_source_extension(relative_path.replace("\\", "/"))
    if value.endswith("/index"):
        value = value[:-6]
    return value.strip("/") or "__root__"


def _qualified_path(path_key: str) -> str:
    """Converts a slash-separated path key into a dotted module name, mirroring Python-style qualified names.

    Input: a normalized `path_key` string from `_path_key`. Called by
    `parse_file` for every non-Java language to derive `module_name` from the
    file's path key, so JS/TS/HTML/CSS modules get dotted names comparable in
    style to Python's (Java instead derives its module name from the file's
    `package` declaration via `_java_module_name`).
    """
    return path_key.replace("/", ".")


def _language_name(path: Path) -> str:
    """Maps a file's extension to the internal language name string used throughout this module.

    Input: a `pathlib.Path`. Returns one of "java", "html", "css",
    "typescript", "tsx", "jsx", or the "javascript" default. Called by
    `parse_file` to decide which tree-sitter grammar (`_language`) to parse
    with and to tag the resulting GraphNode's `metadata["language"]`.
    """
    suffix = path.suffix.lower()
    if suffix == ".java":
        return "java"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix == ".css":
        return "css"
    if suffix in {".ts", ".mts", ".cts"}:
        return "typescript"
    if suffix == ".tsx":
        return "tsx"
    if suffix == ".jsx":
        return "jsx"
    return "javascript"


def _language(language: str) -> Language:
    """Loads the tree-sitter `Language` grammar object for an internal language name.

    Input: one of the language name strings produced by `_language_name`
    ("java", "html", "css", "typescript", "tsx"/"jsx", or default
    "javascript"). Called once per file by `parse_file` to construct the
    `Parser` used to build that file's syntax tree; the TSX grammar is reused
    for plain JSX files since it is a superset that also accepts TypeScript
    syntax.
    """
    if language == "java":
        return Language(tree_sitter_java.language())
    if language == "html":
        return Language(tree_sitter_html.language())
    if language == "css":
        return Language(tree_sitter_css.language())
    if language == "typescript":
        return Language(tree_sitter_typescript.language_typescript())
    if language in {"tsx", "jsx"}:
        # The TSX grammar accepts JSX and TypeScript syntax, making it suitable for both.
        return Language(tree_sitter_typescript.language_tsx())
    return Language(tree_sitter_javascript.language())


@dataclass(slots=True)
class PolyglotModule:
    """Holds one non-Python source file's parsed tree-sitter tree plus the bookkeeping built up while extracting it.

    Created by `PolyglotGraphBuilder.parse_file` (with `root` populated and the
    dict fields empty) and progressively filled in by `_extract_definitions`
    (`definition_nodes`) and the relationship extractors
    (`imported_modules`/`imported_symbols`/`receiver_types`). Kept in
    `PolyglotGraphBuilder.modules`/`modules_by_path`/`modules_by_name` for the
    lifetime of one repository scan so definition and relationship passes can
    share it, mirroring analyzer.py's `ParsedModule`.
    """

    path: Path
    relative_path: str
    path_key: str
    module_name: str
    language: str
    source: bytes
    root: Node
    module_node_id: str
    definition_nodes: dict[tuple[int, int, str], GraphNode] = field(default_factory=dict)
    imported_modules: dict[str, str] = field(default_factory=dict)
    imported_symbols: dict[str, tuple[str, str]] = field(default_factory=dict)
    receiver_types: dict[tuple[str, str], str] = field(default_factory=dict)


class PolyglotGraphBuilder:
    """Tree-sitter extractors that emit Waypoint's language-neutral graph schema."""

    def __init__(self, root: Path, graph: Any, max_file_bytes: int) -> None:
        """Initializes an empty polyglot builder scoped to one repository `root`, sharing `graph` with the Python analyzer pass.

        Input: the repository `root` Path, the `graph` (a
        `GraphAccumulator` from analyzer.py, typed `Any` here to avoid a
        circular import) that Python and polyglot extraction both write into,
        and `max_file_bytes` for the per-file size limit. Constructed once per
        repository scan by `RepositoryAnalyzer.analyze` (backend/app/graph/analyzer.py).
        """
        self.root = root
        self.graph = graph
        self.max_file_bytes = max_file_bytes
        self.modules: list[PolyglotModule] = []
        self.modules_by_path: dict[str, PolyglotModule] = {}
        self.modules_by_name: dict[str, PolyglotModule] = {}
        self.symbols: dict[tuple[str, str], list[GraphNode]] = {}

    @traced("parser.polyglot.parse_file")
    def parse_file(self, path: Path) -> PolyglotModule | None:
        """Parses one non-Python source file with tree-sitter, enforces the byte-size limit, and creates its MODULE GraphNode.

        Input: an absolute file `path` beneath `self.root`. Returns `None`
        (after appending a diagnostic instead of raising) if the file exceeds
        `self.max_file_bytes`, isn't valid UTF-8, or fails to parse; a
        `parse_recovered` warning diagnostic is also recorded (without
        returning `None`) if tree-sitter's error-recovery kicked in, since the
        partial tree is still usable. Called once per discovered non-Python
        file by `RepositoryAnalyzer.analyze`, mirroring `_parse_file` in
        analyzer.py; returning `None` on failure lets one bad file be skipped
        without aborting the whole scan.
        """
        relative = path.relative_to(self.root).as_posix()
        size = path.stat().st_size
        language = _language_name(path)
        log_event(
            logger,
            logging.DEBUG,
            "parser.file_started",
            "Parsing Tree-sitter source file",
            path=relative,
            language=language,
            size_bytes=size,
        )
        if size > self.max_file_bytes:
            self.graph.diagnostics.append(
                AnalysisDiagnostic(
                    severity="warning",
                    code="file_too_large",
                    message=f"File is {size} bytes; limit is {self.max_file_bytes}",
                    path=relative,
                )
            )
            return None
        try:
            source = path.read_bytes()
            source.decode("utf-8")
            tree = Parser(_language(language)).parse(source)
        except (OSError, UnicodeError, ValueError) as exc:
            self.graph.diagnostics.append(
                AnalysisDiagnostic(
                    severity="error",
                    code="parse_failed",
                    message=f"{type(exc).__name__}: {exc}",
                    path=relative,
                )
            )
            log_event(
                logger,
                logging.ERROR,
                "parser.file_failed",
                "Tree-sitter source file could not be parsed",
                path=relative,
                language=language,
                exception_type=type(exc).__name__,
                exception_message=str(exc),
            )
            return None

        path_key = _path_key(relative)
        module_name = self._java_module_name(source, tree.root_node, path) if language == "java" else _qualified_path(path_key)
        line_count = max(1, len(source.decode("utf-8").splitlines()))
        module_span = SourceSpan(
            path=relative,
            start_line=1,
            start_column=0,
            end_line=line_count,
            end_column=0,
        )
        module_node = GraphNode(
            id=_stable_id(NodeKind.MODULE, language, module_name, relative),
            kind=NodeKind.MODULE,
            name=path.stem,
            qualified_name=module_name,
            module=module_name,
            span=module_span,
            metadata={
                "language": language,
                "size_bytes": size,
                "content_sha256": hashlib.sha256(source).hexdigest(),
                "tree_sitter_has_error": tree.root_node.has_error,
            },
        )
        self.graph.add_node(module_node)
        module = PolyglotModule(
            path=path,
            relative_path=relative,
            path_key=path_key,
            module_name=module_name,
            language=language,
            source=source,
            root=tree.root_node,
            module_node_id=module_node.id,
        )
        self.modules.append(module)
        self.modules_by_path[path_key] = module
        self.modules_by_name[module_name] = module
        if tree.root_node.has_error:
            self.graph.diagnostics.append(
                AnalysisDiagnostic(
                    severity="warning",
                    code="parse_recovered",
                    message=(
                        f"Tree-sitter recovered a partial {language} syntax tree; "
                        "extracted nodes remain source-backed but may be incomplete"
                    ),
                    path=relative,
                )
            )
        log_event(
            logger,
            logging.INFO,
            "parser.file_completed",
            "Tree-sitter source file parsed successfully",
            path=relative,
            language=language,
            module=module_name,
            recovered_from_errors=tree.root_node.has_error,
        )
        return module

    @staticmethod
    def _java_module_name(source: bytes, root: Node, path: Path) -> str:
        """Derives a Java file's module name from its `package` declaration (falling back to just the file stem).

        Input: the file's `source` bytes, the tree-sitter `root` node, and the
        file `path`. Finds the first `package_declaration` child of the root
        (if any), strips the `package`/`;` boilerplate from its text, and joins
        it with the file's stem (e.g. `com.example.Foo`); returns just the stem
        if there's no package declaration. Called by `parse_file` only for
        `.java` files, since Java's module identity is conventionally
        package-qualified rather than path-qualified.
        """
        package = next(
            (child for child in root.named_children if child.type == "package_declaration"),
            None,
        )
        if package is None:
            return path.stem
        package_text = _text(source, package).removeprefix("package").rstrip("; ").strip()
        return f"{package_text}.{path.stem}" if package_text else path.stem

    def attach_module(self, module: PolyglotModule, repository_node: GraphNode) -> None:
        """Links a parsed module to the repository with a CONTAINS edge, then runs the appropriate definition extractor for its language.

        Input: a `PolyglotModule` from `parse_file` and the analysis's single
        `repository_node`. Dispatches to `_extract_declarative_definitions` for
        HTML/CSS (which don't have code-like definitions) or
        `_extract_definitions` for JS/TS/Java. Called once per non-Python file
        by `RepositoryAnalyzer.analyze`, right after `parse_file` succeeds.
        """
        module_node = self.graph.nodes[module.module_node_id]
        self.graph.add_edge(
            GraphEdge(
                id=_stable_id(EdgeKind.CONTAINS, repository_node.id, module_node.id),
                source=repository_node.id,
                target=module_node.id,
                kind=EdgeKind.CONTAINS,
                evidence=Evidence(
                    status=EvidenceStatus.VERIFIED,
                    span=module_node.span,
                    syntax=module.relative_path,
                    resolution=f"{module.language} file discovered beneath repository root",
                    confidence=1.0,
                ),
            )
        )
        if module.language in {"html", "css"}:
            self._extract_declarative_definitions(module)
        else:
            self._extract_definitions(module)

    @staticmethod
    def _offset_span(module: PolyglotModule, start: int, end: int) -> SourceSpan:
        """Builds a 1-indexed SourceSpan from raw byte offsets into a module's source, for extraction paths that use regex instead of tree-sitter node positions.

        Input: the `module` (for its source bytes and relative path) and a
        `[start, end)` byte range. Counts newlines before `start` to get the
        starting line/column and within `[start, end)` to get the ending
        line/column. Called by `_add_declarative_symbol` and
        `_extract_declarative_relationships` (the HTML/CSS regex-based
        extractors), which operate on raw text offsets rather than tree-sitter
        node positions.
        """
        before = module.source[:start].decode("utf-8", errors="replace")
        matched = module.source[start:end].decode("utf-8", errors="replace")
        start_line = before.count("\n") + 1
        start_column = len(before.rsplit("\n", 1)[-1])
        end_line = start_line + matched.count("\n")
        end_column = len(matched.rsplit("\n", 1)[-1]) if "\n" in matched else start_column + len(matched)
        return SourceSpan(path=module.relative_path, start_line=start_line, start_column=start_column, end_line=end_line, end_column=end_column)

    def _add_declarative_symbol(self, module: PolyglotModule, name: str, start: int, end: int, declaration_type: str) -> None:
        """Creates a CLASS-kind GraphNode for an HTML id/class selector or CSS selector, deduplicating by id, plus a CONTAINS edge.

        Input: the `module`, the symbol's synthetic `name` (e.g. `"#header"` or
        `".btn"`), its `[start, end)` byte range, and a `declaration_type` tag
        (e.g. `"html_div_id"`, `"css_selector"`) stored in metadata. HTML/CSS
        symbols reuse `NodeKind.CLASS` since they act as addressable named
        targets, analogous to a class, in this schema. Called by
        `_extract_declarative_definitions` for every id/class attribute or CSS
        selector found; silently skips re-adding a node whose id already
        exists, since the same selector can appear in multiple tags/rules.
        """
        module_node = self.graph.nodes[module.module_node_id]
        span = self._offset_span(module, start, end)
        qualified = f"{module.module_name}.{name}"
        node = GraphNode(
            id=_stable_id(NodeKind.CLASS, module.language, qualified, span.start_line),
            kind=NodeKind.CLASS,
            name=name,
            qualified_name=qualified,
            module=module.module_name,
            span=span,
            metadata={"language": module.language, "declaration_type": declaration_type},
        )
        if node.id in self.graph.nodes:
            return
        self.graph.add_node(node)
        self.graph.add_edge(GraphEdge(
            id=_stable_id(EdgeKind.CONTAINS, module_node.id, node.id),
            source=module_node.id,
            target=node.id,
            kind=EdgeKind.CONTAINS,
            evidence=Evidence(status=EvidenceStatus.VERIFIED, span=span, syntax=name, resolution="Tree-sitter-validated declarative structure", confidence=1.0),
        ))

    def _extract_declarative_definitions(self, module: PolyglotModule) -> None:
        """Regex-scans an HTML or CSS file's raw text for addressable id/class/selector "definitions" and records each as a symbol.

        Input: a `module` whose `language` is "html" or "css". For HTML, finds
        every opening tag and, within its attributes, an `id="..."` (recorded
        as `#id`) and/or one or more `class="..."` names (each recorded as
        `.name`). For CSS, finds every rule header (`selector {`) and records
        each comma-separated selector, capped at 160 characters to avoid
        pathological matches. Called by `attach_module` for HTML/CSS files,
        since these languages have no tree-sitter-derived function/class
        concept comparable to code, so this uses direct regex scanning instead.
        """
        text = module.source.decode("utf-8", errors="replace")
        if module.language == "html":
            for match in re.finditer(r"<([A-Za-z][\w:-]*)([^<>]*)>", text):  # matches an HTML opening tag, capturing the tag name and its raw attribute text
                tag, attributes = match.group(1).lower(), match.group(2)
                identifier = re.search(r"\bid\s*=\s*['\"]([^'\"]+)['\"]", attributes, re.I)  # matches an id="..." or id='...' attribute, capturing the id value
                if identifier:
                    self._add_declarative_symbol(module, f"#{identifier.group(1)}", match.start(), match.end(), f"html_{tag}_id")
                classes = re.search(r"\bclass\s*=\s*['\"]([^'\"]+)['\"]", attributes, re.I)  # matches a class="..." or class='...' attribute, capturing the space-separated class list
                if classes:
                    for class_name in classes.group(1).split():
                        self._add_declarative_symbol(module, f".{class_name}", match.start(), match.end(), f"html_{tag}_class")
        else:
            for match in re.finditer(r"(?m)([^@{}][^{}]*)\{", text):  # matches a CSS rule header: one or more selectors (not starting with @, e.g. skips @media/@font-face) up to the opening brace
                for selector in match.group(1).split(","):
                    normalized = " ".join(selector.split()).strip()
                    if normalized and len(normalized) <= 160:
                        self._add_declarative_symbol(module, normalized, match.start(), match.end(), "css_selector")

    def _resolve_asset_module(self, module: PolyglotModule, reference: str) -> PolyglotModule | None:
        """Resolves an HTML/CSS asset reference (e.g. `src="./foo.js"`) to an indexed PolyglotModule, or `None` if it isn't a local path.

        Input: the referencing `module` and the raw `reference` string. Strips
        any query string/fragment, bails out (`None`) for absolute URLs
        (`http(s)://`, protocol-relative `//`, or `data:` URIs) since those
        can't be indexed modules, otherwise resolves the reference relative to
        the referencing module's directory and looks it up by `_path_key` in
        `self.modules_by_path`. Called by `_extract_declarative_relationships`
        for every `src`/`href`/`@import` reference found in HTML/CSS.
        """
        clean = reference.split("?", 1)[0].split("#", 1)[0]
        if not clean or clean.startswith(("http://", "https://", "//", "data:")):
            return None
        base = posixpath.dirname(module.relative_path)
        candidate = _path_key(posixpath.normpath(posixpath.join(base, clean)))
        return self.modules_by_path.get(candidate)

    def _extract_declarative_relationships(self, module: PolyglotModule) -> None:
        """Regex-scans an HTML or CSS file for `src`/`href` or `@import` references and emits IMPORTS edges or unresolved external references.

        Input: a `module` whose `language` is "html" or "css". For HTML,
        matches any `src="..."`/`href="..."` attribute value; for CSS, matches
        `@import` statements (with or without a `url(...)` wrapper). Each
        reference is resolved via `_resolve_asset_module`: a local match
        becomes a VERIFIED IMPORTS edge, while an unresolved reference that
        looks like a full URL is recorded as an external unresolved "import"
        (bare relative paths that don't resolve are silently dropped, since
        they're likely broken links rather than genuine external deps). Called
        by `_extract_relationships` for HTML/CSS modules.
        """
        text = module.source.decode("utf-8", errors="replace")
        if module.language == "html":
            matches = re.finditer(r"\b(?:src|href)\s*=\s*['\"]([^'\"]+)['\"]", text, re.I)  # matches an HTML src="..." or href="..." attribute, capturing the referenced path/URL
        else:
            matches = re.finditer(r"@import\s+(?:url\()?\s*['\"]([^'\"]+)['\"]", text, re.I)  # matches a CSS @import statement, optionally wrapped in url(...), capturing the imported path
        for match in matches:
            reference = match.group(1)
            target = self._resolve_asset_module(module, reference)
            span = self._offset_span(module, match.start(), match.end())
            if target is not None:
                target_node = self.graph.nodes[target.module_node_id]
                self.graph.add_edge(GraphEdge(
                    id=_stable_id(EdgeKind.IMPORTS, module.module_node_id, target.module_node_id, span.start_line, reference),
                    source=module.module_node_id,
                    target=target.module_node_id,
                    kind=EdgeKind.IMPORTS,
                    evidence=Evidence(status=EvidenceStatus.VERIFIED, span=span, syntax=match.group(0), resolution="Resolved local HTML/CSS asset reference", confidence=1.0),
                ))
            elif reference.startswith(("http://", "https://", "//")):
                self.graph.add_unresolved(UnresolvedReference(
                    source=module.module_node_id,
                    reference=reference,
                    reference_kind="import",
                    evidence=Evidence(status=EvidenceStatus.UNRESOLVED, span=span, syntax=match.group(0), resolution="External web asset", confidence=0.0),
                    metadata={"external": True, "package": reference},
                ))

    def _definition(self, module: PolyglotModule, node: Node, parent: GraphNode) -> tuple[NodeKind, str, str | None] | None:
        """Classifies a single tree-sitter node as a class/function/method definition (or `None` if it isn't one).

        Input: the `module` (for language/source), the candidate `node`, and
        its lexical `parent` GraphNode (used to decide FUNCTION vs. METHOD).
        Recognizes: class-like declarations (`_CLASS_TYPES`, defaulting an
        unnamed default-exported class to the name `"default"`); Java methods/
        constructors (`_JAVA_METHOD_TYPES`); JS/TS function declarations
        (`_JS_FUNCTION_TYPES`) and method definitions (`_JS_METHOD_TYPES`);
        `const x = () => {...}`-style arrow/function-expression variable
        declarators; and class field definitions initialized to a function
        expression. Returns `(kind, name, signature)` or `None`. Called by
        `_extract_definitions`'s inner `walk` for every node in the tree; this
        is the single place that decides what counts as a "definition" across
        Java, JavaScript, TypeScript, and JSX/TSX.
        """
        name_node = node.child_by_field_name("name")
        if node.type in _CLASS_TYPES:
            name = _text(module.source, name_node)
            if not name and node.parent and node.parent.type == "export_statement":
                name = "default"
            if name:
                return NodeKind.CLASS, name, None
        if module.language == "java" and node.type in _JAVA_METHOD_TYPES and name_node is not None:
            parameters = node.child_by_field_name("parameters")
            return NodeKind.METHOD, _text(module.source, name_node), _text(module.source, parameters)
        if module.language != "java" and node.type in _JS_FUNCTION_TYPES:
            parameters = node.child_by_field_name("parameters")
            kind = NodeKind.METHOD if parent.kind == NodeKind.CLASS else NodeKind.FUNCTION
            name = _text(module.source, name_node)
            if not name and node.parent and node.parent.type == "export_statement":
                name = "default"
            if name:
                return kind, name, _text(module.source, parameters)
        if module.language != "java" and node.type in _JS_METHOD_TYPES and name_node is not None:
            parameters = node.child_by_field_name("parameters")
            return NodeKind.METHOD, _text(module.source, name_node), _text(module.source, parameters)
        if module.language != "java" and node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is not None and value.type in {"arrow_function", "function_expression", "generator_function"}:
                variable = node.child_by_field_name("name")
                if variable is not None and variable.type in {"identifier", "property_identifier"}:
                    parameters = value.child_by_field_name("parameters")
                    kind = NodeKind.METHOD if parent.kind == NodeKind.CLASS else NodeKind.FUNCTION
                    return kind, _text(module.source, variable), _text(module.source, parameters)
        if module.language != "java" and node.type in {"public_field_definition", "field_definition"}:
            value = node.child_by_field_name("value")
            variable = node.child_by_field_name("name")
            if value is not None and variable is not None and value.type in {"arrow_function", "function_expression"}:
                parameters = value.child_by_field_name("parameters")
                return NodeKind.METHOD, _text(module.source, variable), _text(module.source, parameters)
        return None

    def _extract_definitions(self, module: PolyglotModule) -> None:
        """Recursively walks a JS/TS/Java module's tree, emitting a GraphNode + CONTAINS edge for every class/function/method found.

        Input: a `module` whose `language` is not "html"/"css". Defines and
        runs a local `walk(node, parent, qualifiers)` closure over
        `module.root`: for each node, `_definition` decides whether it's a
        definition; if so, a GraphNode is created (id derived from kind,
        language, qualified name, and span), linked to `parent` via CONTAINS,
        recorded in `module.definition_nodes` (keyed by `_node_key`) for later
        relationship resolution, and becomes the new `parent`/qualifier prefix
        for its own children. Called by `attach_module` for JS/TS/Java files;
        this is the polyglot equivalent of analyzer.py's `DefinitionVisitor`.
        """
        module_node = self.graph.nodes[module.module_node_id]

        def walk(node: Node, parent: GraphNode, qualifiers: list[str]) -> None:
            """Recursively visits `node` and its children, recording any definition found; see `_extract_definitions` above for the full behavior."""
            definition = self._definition(module, node, parent)
            next_parent = parent
            next_qualifiers = qualifiers
            if definition is not None:
                kind, name, signature = definition
                source_span = _span(module.relative_path, node)
                qualified = ".".join([module.module_name, *qualifiers, name])
                graph_node = GraphNode(
                    id=_stable_id(kind, module.language, qualified, source_span.path, source_span.start_line),
                    kind=kind,
                    name=name,
                    qualified_name=qualified,
                    module=module.module_name,
                    span=source_span,
                    signature=signature or None,
                    metadata={"language": module.language, "declaration_type": node.type},
                )
                self.graph.add_node(graph_node)
                self.graph.add_edge(
                    GraphEdge(
                        id=_stable_id(EdgeKind.CONTAINS, parent.id, graph_node.id),
                        source=parent.id,
                        target=graph_node.id,
                        kind=EdgeKind.CONTAINS,
                        evidence=Evidence(
                            status=EvidenceStatus.VERIFIED,
                            span=source_span,
                            syntax=f"{node.type} {name}",
                            resolution="Tree-sitter lexical containment",
                            confidence=1.0,
                        ),
                    )
                )
                module.definition_nodes[_node_key(node)] = graph_node
                next_parent = graph_node
                next_qualifiers = [*qualifiers, name]
            for child in node.named_children:
                walk(child, next_parent, next_qualifiers)

        walk(module.root, module_node, [])

    def _index_symbols(self) -> None:
        """Rebuilds `self.symbols`, a (module_name, name-or-qualified_name) -> [GraphNode] index over every module's definitions.

        No inputs beyond `self`; clears and repopulates `self.symbols` from
        every `PolyglotModule.definition_nodes` collected so far, indexing each
        definition under both its bare `name` and its `qualified_name` so
        lookups can match either. Called once by `resolve_relationships` before
        any relationship extraction begins, since name resolution
        (`_resolve_symbol`, `_resolve_member`, `_unique_imported_member`) needs
        every module's definitions to already be indexed, not just the current
        module's.
        """
        self.symbols.clear()
        for module in self.modules:
            for node in module.definition_nodes.values():
                self.symbols.setdefault((module.module_name, node.name), []).append(node)
                self.symbols.setdefault((module.module_name, node.qualified_name), []).append(node)

    def _resolve_js_module(self, module: PolyglotModule, specifier: str) -> PolyglotModule | None:
        """Resolves a relative JS/TS import specifier (e.g. `"./utils"`) to an indexed PolyglotModule.

        Input: the importing `module` and the raw `specifier` string; returns
        `None` immediately for non-relative specifiers (bare package names like
        `"react"`), since only relative imports can be resolved to files in
        this repository. Joins the specifier onto the importing module's
        directory, strips any source extension, and looks it up by `_path_key`
        directly or as a `.../index` file. Called by `_extract_js_import` and
        `_extract_js_require` for every import/require statement.
        """
        if not specifier.startswith("."):
            return None
        base = posixpath.dirname(_strip_source_extension(module.relative_path))
        candidate = _strip_source_extension(posixpath.normpath(posixpath.join(base, specifier)))
        return self.modules_by_path.get(candidate) or self.modules_by_path.get(candidate.rstrip("/") + "/index")

    def _add_import(self, module: PolyglotModule, target: PolyglotModule, node: Node, resolution: str) -> None:
        """Emits a VERIFIED IMPORTS edge from `module` to an already-resolved `target` module.

        Input: the importing `module`, the resolved `target` PolyglotModule,
        the originating `node` (for span/syntax), and a `resolution`
        explanation string. Called by `_extract_js_import`,
        `_extract_js_require`, `_extract_js_reexport`, and
        `_extract_java_import` once each has confirmed the import target is an
        indexed internal module.
        """
        source_span = _span(module.relative_path, node)
        self.graph.add_edge(
            GraphEdge(
                id=_stable_id(EdgeKind.IMPORTS, module.module_node_id, target.module_node_id, source_span.start_line, _text(module.source, node)),
                source=module.module_node_id,
                target=target.module_node_id,
                kind=EdgeKind.IMPORTS,
                evidence=Evidence(
                    status=EvidenceStatus.VERIFIED,
                    span=source_span,
                    syntax=_text(module.source, node),
                    resolution=resolution,
                    confidence=1.0,
                ),
            )
        )

    @staticmethod
    def _javascript_package(specifier: str) -> str:
        """Extracts the installable package name from a JS/TS import specifier, for tagging unresolved external imports.

        Input: a non-relative import `specifier` string. Handles scoped
        packages (`@scope/name/...` -> `@scope/name`), Node builtin imports
        (`node:fs` kept as-is), and plain packages (`lodash/debounce` ->
        `lodash`). Called by `_extract_js_import`, `_extract_js_require`, and
        `_extract_js_reexport` to populate `metadata["package"]` on unresolved
        external references, so the UI can group/report dependencies by
        package rather than by full import path.
        """
        if specifier.startswith("@"):
            return "/".join(specifier.split("/")[:2])
        if specifier.startswith("node:"):
            return specifier
        return specifier.split("/", 1)[0]

    @staticmethod
    def _java_package(reference: str) -> str:
        """Extracts the package-name prefix from a Java import reference, stopping before the first capitalized (type) segment.

        Input: a raw Java import `reference` string (e.g.
        `com.example.Foo.Bar` or `com.example.*`). Strips a trailing `.*`
        wildcard, then walks dotted segments and stops at the first one
        starting with an uppercase letter (Java's convention for type names),
        returning everything before it joined back with dots (or the whole
        reference if no such segment is found). Called by `_extract_java_import`
        to populate `metadata["package"]` on unresolved external imports.
        """
        parts = reference.removesuffix(".*").split(".")
        package_parts: list[str] = []
        for part in parts:
            if part and part[0].isupper():
                break
            package_parts.append(part)
        return ".".join(package_parts) or reference

    def _unresolved(
        self,
        module: PolyglotModule,
        source_id: str,
        node: Node,
        reference: str,
        kind: str,
        resolution: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Builds and stores an UnresolvedReference with UNRESOLVED evidence for an import/call/instantiation that couldn't be matched.

        Input: the `module` (for span/syntax context), the `source_id` node
        that made the reference, the originating `node`, a human-readable
        `reference` string, its `kind` ("import"/"call"/"instantiation"), a
        `resolution` explanation, and optional `metadata` (e.g.
        `external`/`package` flags). Called throughout this file's import/call/
        constructor resolution paths (`_extract_js_import`,
        `_extract_java_import`, `_add_call`, `_add_instantiation`, etc.) as the
        polyglot equivalent of analyzer.py's `_record_unresolved`, feeding
        `AnalysisStats.unresolved_count` and `AnalysisReport.unresolved_references`.
        """
        self.graph.add_unresolved(
            UnresolvedReference(
                source=source_id,
                reference=reference,
                reference_kind=kind,
                evidence=Evidence(
                    status=EvidenceStatus.UNRESOLVED,
                    span=_span(module.relative_path, node),
                    syntax=_text(module.source, node),
                    resolution=resolution,
                    confidence=0.0,
                ),
                metadata=metadata or {},
            )
        )

    def _extract_js_import(self, module: PolyglotModule, node: Node) -> None:
        """Resolves one ES module `import ... from "..."` statement, recording alias/symbol maps and emitting IMPORTS edges or an unresolved reference.

        Input: the `module` and an `import_statement` tree-sitter `node`. If the
        specifier's `source` field is missing, does nothing (defensive
        no-op). Resolves the specifier via `_resolve_js_module`; on failure,
        records an unresolved "import" flagged external/internal-unmappable. On
        success, emits an IMPORTS edge, then walks the `import_clause` to
        record: a bare default-import identifier as an alias to the target's
        sole class (if unambiguous), each named `import_specifier` (`{ a, b as
        c }`) as an imported symbol, and a `namespace_import` (`* as ns`) as a
        module alias. Called by `_extract_relationships` for every
        `import_statement` node in a JS/TS module.
        """
        source_node = node.child_by_field_name("source")
        if source_node is None:
            return
        specifier = _text(module.source, source_node).strip("'\"")
        target = self._resolve_js_module(module, specifier)
        if target is None:
            external = not specifier.startswith(".")
            self._unresolved(
                module,
                module.module_node_id,
                node,
                specifier,
                "import",
                "External ECMAScript package import" if external else "Relative import cannot be mapped to an indexed module",
                metadata={
                    "external": external,
                    "package": self._javascript_package(specifier) if external else specifier,
                },
            )
            return
        self._add_import(module, target, node, "Resolved relative ECMAScript module import")
        clause = next((child for child in node.named_children if child.type == "import_clause"), None)
        if clause is None:
            return
        direct_identifiers = [child for child in clause.named_children if child.type == "identifier"]
        if direct_identifiers:
            local = _text(module.source, direct_identifiers[0])
            class_candidates = [item for item in target.definition_nodes.values() if item.kind == NodeKind.CLASS]
            if len(class_candidates) == 1:
                module.imported_symbols[local] = (target.module_name, class_candidates[0].name)
        for spec in _descendants(clause, {"import_specifier"}):
            original = spec.child_by_field_name("name")
            alias = spec.child_by_field_name("alias")
            if original is not None:
                module.imported_symbols[_text(module.source, alias or original)] = (target.module_name, _text(module.source, original))
        namespace = next(_descendants(clause, {"namespace_import"}), None)
        if namespace is not None:
            identifiers = [child for child in namespace.named_children if child.type == "identifier"]
            if identifiers:
                module.imported_modules[_text(module.source, identifiers[-1])] = target.module_name

    def _extract_js_require(self, module: PolyglotModule, node: Node) -> bool:
        """Resolves one CommonJS `require("...")` call expression, recording alias/destructure maps and emitting an IMPORTS edge.

        Input: the `module` and a `call_expression` tree-sitter `node`. Returns
        `False` if this call isn't actually a `require(...)` call (wrong
        function name or missing arguments), so the caller can tell genuine
        `require` calls apart from other call expressions; returns `True`
        otherwise (including when the argument isn't a string literal, which is
        treated as a no-op require). On a resolved target, records the
        enclosing `variable_declarator`'s binding: a plain identifier becomes a
        module alias, while an `object_pattern` (`const { a, b: c } =
        require(...)`) records each destructured name as an imported symbol.
        Called by `_extract_relationships`, which also uses this method's
        return value to build the `require_calls` set so those nodes are
        excluded from generic call-expression handling.
        """
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is None or _text(module.source, function) != "require" or arguments is None:
            return False
        string_node = next(_descendants(arguments, {"string"}), None)
        if string_node is None:
            return True
        specifier = _text(module.source, string_node).strip("'\"")
        target = self._resolve_js_module(module, specifier)
        if target is None:
            external = not specifier.startswith(".")
            self._unresolved(
                module,
                module.module_node_id,
                node,
                specifier,
                "import",
                "External CommonJS package require" if external else "Relative CommonJS require cannot be mapped to an indexed module",
                metadata={
                    "external": external,
                    "package": self._javascript_package(specifier) if external else specifier,
                },
            )
            return True
        self._add_import(module, target, node, "Resolved relative CommonJS require")
        declarator = node.parent if node.parent and node.parent.type == "variable_declarator" else None
        name_node = declarator.child_by_field_name("name") if declarator else None
        if name_node is None:
            return True
        if name_node.type == "identifier":
            module.imported_modules[_text(module.source, name_node)] = target.module_name
        elif name_node.type == "object_pattern":
            for child in name_node.named_children:
                if child.type in {"shorthand_property_identifier_pattern", "identifier"}:
                    name = _text(module.source, child)
                    module.imported_symbols[name] = (target.module_name, name)
                elif child.type == "pair_pattern":
                    key = child.child_by_field_name("key")
                    value = child.child_by_field_name("value")
                    if key is not None and value is not None:
                        module.imported_symbols[_text(module.source, value)] = (target.module_name, _text(module.source, key))
        return True

    def _extract_js_reexport(self, module: PolyglotModule, node: Node) -> None:
        """Resolves one `export ... from "..."` re-export statement to an IMPORTS edge or an unresolved reference.

        Input: the `module` and an `export_statement` tree-sitter `node` that
        has a `source` field (i.e. it re-exports from another module). Mirrors
        `_extract_js_import`'s resolution logic but doesn't need to record
        alias/symbol maps, since re-exports don't introduce new local bindings
        in this module. Called by `_extract_relationships` for every
        `export_statement` node that has a `source` child.
        """
        source_node = node.child_by_field_name("source")
        if source_node is None:
            return
        specifier = _text(module.source, source_node).strip("'\"")
        target = self._resolve_js_module(module, specifier)
        if target is None:
            external = not specifier.startswith(".")
            self._unresolved(
                module,
                module.module_node_id,
                node,
                specifier,
                "import",
                "External ECMAScript package re-export" if external else "Relative re-export cannot be mapped to an indexed module",
                metadata={
                    "external": external,
                    "package": self._javascript_package(specifier) if external else specifier,
                },
            )
            return
        self._add_import(module, target, node, "Resolved ECMAScript re-export to indexed module")

    def _extract_java_import(self, module: PolyglotModule, node: Node) -> None:
        """Resolves one Java `import [static] pkg.Type[.member];` statement, handling wildcard, member, and plain type imports.

        Input: the `module` and an `import_declaration` tree-sitter `node`.
        Strips the `import`/`static`/`;` boilerplate from the raw text; for a
        wildcard import (`pkg.*`), finds every indexed module directly under
        that package and adds an IMPORTS edge to each; otherwise looks up the
        candidate as a module name directly, then (if that fails) as
        `base.member` where `base` is a module and `member` is treated as a
        static-imported symbol. Files an unresolved "import" (flagged
        external/internal based on whether the first two dotted segments match
        any indexed module's package prefix) if nothing resolves. Called by
        `_extract_relationships` for every `import_declaration` node in a Java
        file.
        """
        raw = _text(module.source, node).removeprefix("import").rstrip("; ").strip()
        raw = raw.removeprefix("static ").strip()
        wildcard = raw.endswith(".*")
        candidate = raw[:-2] if wildcard else raw
        if wildcard:
            targets = [
                item
                for name, item in self.modules_by_name.items()
                if name.startswith(candidate + ".")
                and name.count(".") == candidate.count(".") + 1
            ]
            if targets:
                for target_item in targets:
                    self._add_import(module, target_item, node, "Resolved Java wildcard import to indexed source type")
                    module.imported_modules[target_item.module_name.rsplit(".", 1)[-1]] = target_item.module_name
                return
        target = self.modules_by_name.get(candidate)
        imported_member: str | None = None
        if target is None and "." in candidate:
            base, member = candidate.rsplit(".", 1)
            target = self.modules_by_name.get(base)
            if target is not None:
                imported_member = member
        if target is None:
            internal_prefixes = {
                ".".join(name.split(".")[:2])
                for name in self.modules_by_name
                if "." in name
            }
            candidate_prefix = ".".join(candidate.split(".")[:2])
            external = candidate_prefix not in internal_prefixes
            self._unresolved(
                module,
                module.module_node_id,
                node,
                raw,
                "import",
                "External Java package import" if external else "Java import is in the repository namespace but does not match an indexed source type",
                metadata={
                    "external": external,
                    "package": self._java_package(raw),
                },
            )
            return
        self._add_import(module, target, node, "Resolved Java import to indexed source type")
        type_name = target.module_name.rsplit(".", 1)[-1]
        module.imported_modules[type_name] = target.module_name
        if imported_member:
            module.imported_symbols[imported_member] = (target.module_name, imported_member)

    def _resolve_symbol(self, module_name: str, name: str) -> GraphNode | None:
        """Looks up a single, unambiguous definition named `name` within `module_name` from the pre-built symbol index.

        Input: a `module_name` and a bare or qualified `name`. Returns the sole
        candidate from `self.symbols[(module_name, name)]` only if there is
        exactly one, `None` otherwise (ambiguous or missing). Called throughout
        call/instantiation resolution (`_call_target`, `_constructor_target`)
        wherever a specific module+name pair needs to resolve to one definite
        GraphNode without guessing among duplicates.
        """
        candidates = self.symbols.get((module_name, name), [])
        return candidates[0] if len(candidates) == 1 else None

    def _resolve_member(
        self, module_name: str, class_name: str, member_name: str
    ) -> GraphNode | None:
        """Looks up a unique method/member named `member_name` that belongs specifically to `class_name` within `module_name`.

        Input: `module_name`, the owning `class_name`, and the `member_name`
        being accessed. Filters `self.symbols[(module_name, member_name)]` down
        to candidates whose qualified name ends with `.{class_name}.{member_name}`
        (i.e. actually declared on that class, not just same-named elsewhere in
        the module), requiring exactly one match. Called by `_call_target`'s
        JS/TS branch when a receiver's declared/constructed type is known, to
        resolve `receiver.method()` more precisely than a bare name lookup
        would.
        """
        candidates = [
            candidate
            for candidate in self.symbols.get((module_name, member_name), [])
            if candidate.qualified_name.endswith(f".{class_name}.{member_name}")
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _unique_imported_member(
        self, module: PolyglotModule, name: str
    ) -> GraphNode | None:
        """Infer an unknown receiver only from one unique internal imported member.

        Input: the `module` making the call and the member `name` being
        accessed (e.g. `attr` from `obj.attr(...)`). Called by `_call_target` as
        a last-resort fallback when the receiver's type can't be determined by
        any more specific means (declared type, `this`, imported module alias).
        Mirrors analyzer.py's `RelationshipVisitor._unique_imported_member`;
        exists to still recover call edges for dynamically-typed receivers
        without producing a false-positive edge when multiple same-named
        members exist across the module's imports.
        """
        module_names = set(module.imported_modules.values()) | {
            module_name
            for module_name, _symbol_name in module.imported_symbols.values()
        }
        candidates = {
            candidate.id: candidate
            for module_name in module_names
            for candidate in self.symbols.get((module_name, name), [])
            if candidate.kind in {NodeKind.FUNCTION, NodeKind.METHOD}
        }
        return next(iter(candidates.values())) if len(candidates) == 1 else None

    def _definition_context(self, module: PolyglotModule, node: Node, source_id: str, class_id: str | None) -> tuple[str, str | None]:
        """Updates the current "source" and "enclosing class" ids when traversal enters a node that is itself a recorded definition.

        Input: the `module`, the `node` currently being visited, and the
        `source_id`/`class_id` inherited from the parent traversal frame. If
        `node` was recorded as a definition in `module.definition_nodes`
        (looked up via `_node_key`), returns that definition's id as the new
        `source_id`, and as the new `class_id` too if it's a CLASS; otherwise
        passes the inherited values through unchanged. Called at the start of
        every recursive `walk` step in `_collect_java_receiver_types` and
        `_extract_relationships`, so calls/instantiations are attributed to the
        innermost enclosing function/method rather than the module as a whole.
        """
        definition = module.definition_nodes.get(_node_key(node))
        if definition is None:
            return source_id, class_id
        return definition.id, definition.id if definition.kind == NodeKind.CLASS else class_id

    def _collect_java_receiver_types(self, module: PolyglotModule) -> None:
        """Walks a Java module recording each field's/local variable's/parameter's declared type, keyed by (owning scope, name).

        Input: a `module` whose `language` is "java". Defines and runs a local
        `walk(node, source_id, class_id)` closure over `module.root`: for
        `field_declaration`/`local_variable_declaration` nodes, records each
        declared variable's type under the owning class (for fields) or
        enclosing method (for locals); for `formal_parameter` nodes, records
        the parameter's type under the enclosing method. Populates
        `module.receiver_types`. Called once by `_extract_relationships` before
        walking for calls, since `_call_target`'s Java branch needs to already
        know a receiver's declared type to resolve `receiver.method()` calls.
        """
        def walk(node: Node, source_id: str, class_id: str | None) -> None:
            """Recursively visits `node` and its children, recording field/parameter types; see `_collect_java_receiver_types` above for the full behavior."""
            source_id, class_id = self._definition_context(module, node, source_id, class_id)
            if node.type in {"field_declaration", "local_variable_declaration"}:
                type_node = node.child_by_field_name("type")
                owner = class_id if node.type == "field_declaration" else source_id
                if type_node is not None and owner:
                    for declarator in _descendants(node, {"variable_declarator"}):
                        name_node = declarator.child_by_field_name("name")
                        if name_node is not None:
                            module.receiver_types[(owner, _text(module.source, name_node))] = _text(module.source, type_node)
            if node.type == "formal_parameter":
                type_node = node.child_by_field_name("type")
                name_node = node.child_by_field_name("name")
                if type_node is not None and name_node is not None:
                    module.receiver_types[(source_id, _text(module.source, name_node))] = _text(module.source, type_node)
            for child in node.named_children:
                walk(child, source_id, class_id)

        walk(module.root, module.module_node_id, None)

    def _call_target(
        self,
        module: PolyglotModule,
        node: Node,
        source_id: str,
        class_id: str | None,
    ) -> tuple[GraphNode | None, str, str, float]:
        """Attempts to resolve a Java `method_invocation` or JS/TS `call_expression`/`subscript_expression` node to a specific GraphNode.

        Input: the `module`, the call `node`, the id of the enclosing
        definition (`source_id`), and the id of the enclosing class if any
        (`class_id`). For Java: handles unqualified calls (same source type or
        unique same-file method), `this.method()`, and `receiver.method()` where
        the receiver's type comes from an import alias or a declared field/
        local/parameter type (via `receiver_types`), each at 0.8 confidence,
        falling back to `_unique_imported_member` at 0.55. For JS/TS: handles a
        bare identifier call (imported symbol or same-module, 0.8) and
        `object.property(...)`/`object[property](...)` member calls — `this.x`
        (0.8), an imported-namespace member (0.8), a member resolved through a
        known receiver type via `_resolve_member` (0.8), or the
        `_unique_imported_member` fallback (0.55) — returning `(None, ...,
        0.0)` when nothing statically establishes the target. Called by
        `_add_call` for every recognized call-expression node; this is the
        polyglot analogue of analyzer.py's `RelationshipVisitor._resolve_call`.
        """
        if module.language == "java":
            name_node = node.child_by_field_name("name")
            name = _text(module.source, name_node)
            object_node = node.child_by_field_name("object")
            if object_node is None:
                if class_id:
                    class_node = self.graph.nodes.get(class_id)
                    if class_node:
                        target = self._resolve_symbol(module.module_name, name)
                        if target:
                            return target, name, "Resolved unqualified Java method in the same source type", 0.8
                return self._resolve_symbol(module.module_name, name), name, "Resolved unique same-file Java method", 0.8
            receiver = _text(module.source, object_node)
            if receiver == "this" and class_id:
                return self._resolve_symbol(module.module_name, name), f"this.{name}", "Resolved method on lexical Java class", 0.8
            target_module_name = module.imported_modules.get(receiver)
            if target_module_name is None:
                receiver_type = module.receiver_types.get((source_id, receiver))
                if receiver_type is None and class_id:
                    receiver_type = module.receiver_types.get((class_id, receiver))
                if receiver_type:
                    target_module_name = module.imported_modules.get(receiver_type)
                    if target_module_name is None:
                        same_package = module.module_name.rsplit(".", 1)[0] + "." + receiver_type
                        if same_package in self.modules_by_name:
                            target_module_name = same_package
            if target_module_name:
                return self._resolve_symbol(target_module_name, name), f"{receiver}.{name}", "Resolved Java receiver using imported or declared static type", 0.8
            recovered = self._unique_imported_member(module, name)
            if recovered is not None:
                return recovered, f"{receiver}.{name}", "Conservatively inferred unique matching Java member across internal imports", 0.55
            return None, f"{receiver}.{name}", "Java receiver type is not statically established", 0.0

        function = node.child_by_field_name("function")
        if function is None:
            return None, _text(module.source, node), "Call has no named function expression", 0.0
        if function.type == "identifier":
            name = _text(module.source, function)
            imported = module.imported_symbols.get(name)
            if imported:
                return self._resolve_symbol(*imported), name, "Resolved imported ECMAScript symbol alias", 0.8
            return self._resolve_symbol(module.module_name, name), name, "Resolved unique same-module symbol", 0.8
        if function.type in {"member_expression", "subscript_expression"}:
            object_node = function.child_by_field_name("object")
            property_node = function.child_by_field_name("property")
            receiver = _text(module.source, object_node)
            name = _text(module.source, property_node)
            if receiver == "this" and class_id:
                return self._resolve_symbol(module.module_name, name), f"this.{name}", "Resolved method on lexical class", 0.8
            target_module_name = module.imported_modules.get(receiver)
            if target_module_name:
                return self._resolve_symbol(target_module_name, name), f"{receiver}.{name}", "Resolved member on imported ECMAScript namespace", 0.8
            receiver_type = module.receiver_types.get((source_id, receiver))
            if receiver_type is None and class_id:
                receiver_type = module.receiver_types.get((class_id, receiver))
            if receiver_type:
                imported_type = module.imported_symbols.get(receiver_type)
                target_module_name = (
                    imported_type[0]
                    if imported_type
                    else module.imported_modules.get(receiver_type)
                )
                if target_module_name:
                    target = self._resolve_member(
                        target_module_name, receiver_type, name
                    )
                    if target:
                        return (
                            target,
                            f"{receiver}.{name}",
                            "Resolved member through locally constructed receiver",
                            0.8,
                        )
            recovered = self._unique_imported_member(module, name)
            if recovered is not None:
                return recovered, f"{receiver}.{name}", "Conservatively inferred unique matching ECMAScript member across internal imports", 0.55
            return None, f"{receiver}.{name}", "Member receiver type is not statically established", 0.0
        return None, _text(module.source, function), "Dynamic call expression is not statically resolvable", 0.0

    def _constructor_target(
        self, module: PolyglotModule, node: Node
    ) -> tuple[GraphNode | None, str, str]:
        """Attempts to resolve a `new Foo(...)`/object-creation node's constructed type to a specific CLASS GraphNode.

        Input: the `module` and a `new_expression`/`object_creation_expression`
        tree-sitter `node`. Reads the type name from the node's `constructor`
        or `type` field (whichever is present), strips any generic type
        arguments (`<...>`) and namespace qualification down to the short name,
        then tries in order: an imported-symbol alias, an imported-module
        alias, and finally a same-module lookup — accepting a match only if it
        resolves to a `NodeKind.CLASS`. Called by `_add_instantiation` for every
        constructor call found while walking a module.
        """
        constructor = (
            node.child_by_field_name("constructor")
            or node.child_by_field_name("type")
        )
        if constructor is None:
            return None, _text(module.source, node), "Construction has no named type"
        name = _text(module.source, constructor).split("<", 1)[0].strip()
        short_name = name.rsplit(".", 1)[-1]
        imported = module.imported_symbols.get(short_name)
        if imported:
            target = self._resolve_symbol(imported[0], imported[1])
            if target and target.kind == NodeKind.CLASS:
                return target, name, "Resolved imported class construction"
        target_module = module.imported_modules.get(short_name)
        if target_module:
            target = self._resolve_symbol(target_module, short_name)
            if target and target.kind == NodeKind.CLASS:
                return target, name, "Resolved imported type construction"
        target = self._resolve_symbol(module.module_name, short_name)
        if target and target.kind == NodeKind.CLASS:
            return target, name, "Resolved same-module class construction"
        return None, name, "Constructed type is not uniquely resolvable"

    def _add_instantiation(
        self, module: PolyglotModule, node: Node, source_id: str
    ) -> GraphNode | None:
        """Resolves a constructor-call node via `_constructor_target` and records either an INSTANTIATES edge or an unresolved reference.

        Input: the `module`, the `new_expression`/`object_creation_expression`
        `node`, and the `source_id` of the enclosing definition. On success,
        adds an INFERRED INSTANTIATES edge (fixed 0.82 confidence) from
        `source_id` to the resolved class and returns that class's GraphNode;
        on failure, records an unresolved "instantiation" reference and returns
        `None`. Called by `_extract_relationships`'s inner `walk` for every
        `new_expression`/`object_creation_expression` node; the returned
        GraphNode is also used by the caller to bind a variable's receiver type
        when the instantiation is directly assigned.
        """
        target, reference, resolution = self._constructor_target(module, node)
        if target is None:
            self._unresolved(
                module, source_id, node, reference, "instantiation", resolution
            )
            return None
        source_span = _span(module.relative_path, node)
        self.graph.add_edge(
            GraphEdge(
                id=_stable_id(
                    EdgeKind.INSTANTIATES,
                    source_id,
                    target.id,
                    source_span.start_line,
                    source_span.start_column,
                ),
                source=source_id,
                target=target.id,
                kind=EdgeKind.INSTANTIATES,
                evidence=Evidence(
                    status=EvidenceStatus.INFERRED,
                    span=source_span,
                    syntax=_text(module.source, node),
                    resolution=resolution,
                    confidence=0.82,
                ),
                metadata={"call_site_kind": "instantiation"},
            )
        )
        return target

    def _add_call(self, module: PolyglotModule, node: Node, source_id: str, class_id: str | None) -> None:
        """Resolves a call-expression node via `_call_target` and records either a MAY_CALL edge or an unresolved "call" reference.

        Input: the `module`, the call `node`, and the enclosing `source_id`/
        `class_id`. On success, adds an INFERRED MAY_CALL edge at the
        confidence `_call_target` returned; on failure, records an unresolved
        "call" reference via `_unresolved`. Called by `_extract_relationships`'s
        inner `walk` for every Java `method_invocation` or JS/TS
        `call_expression` node that isn't itself a `require(...)` call already
        handled by `_extract_js_require`.
        """
        target, reference, resolution, confidence = self._call_target(
            module, node, source_id, class_id
        )
        source_span = _span(module.relative_path, node)
        if target is None:
            self._unresolved(module, source_id, node, reference, "call", resolution)
            return
        self.graph.add_edge(
            GraphEdge(
                id=_stable_id(EdgeKind.MAY_CALL, source_id, target.id, source_span.start_line, source_span.start_column),
                source=source_id,
                target=target.id,
                kind=EdgeKind.MAY_CALL,
                evidence=Evidence(
                    status=EvidenceStatus.INFERRED,
                    span=source_span,
                    syntax=_text(module.source, node),
                    resolution=resolution,
                    confidence=confidence,
                ),
            )
        )

    def _extract_relationships(self, module: PolyglotModule) -> None:
        """Extracts every import/call/instantiation relationship for one module, dispatching by language, and walks the tree once for calls/constructions.

        Input: a `module` of any supported language. For HTML/CSS, delegates
        entirely to `_extract_declarative_relationships` and returns. For Java,
        extracts every `import_declaration` then runs
        `_collect_java_receiver_types` before walking for
        `method_invocation` calls. For JS/TS, extracts every `import_statement`
        and source-bearing `export_statement` (re-export), and pre-identifies
        `require(...)` call nodes (`require_calls`) so the generic call walk
        below can skip them (they're already handled as imports). The inner
        `walk` closure then recurses over the tree, calling
        `_add_instantiation` for `new_expression`/`object_creation_expression`
        nodes (binding a variable's receiver type when the result is directly
        assigned) and `_add_call` for recognized call-expression nodes not
        already consumed as a require. Called once per module by
        `resolve_relationships`.
        """
        if module.language in {"html", "css"}:
            self._extract_declarative_relationships(module)
            return
        if module.language == "java":
            for node in _descendants(module.root, {"import_declaration"}):
                self._extract_java_import(module, node)
            self._collect_java_receiver_types(module)
            call_types = {"method_invocation"}
        else:
            for node in _descendants(module.root, {"import_statement"}):
                self._extract_js_import(module, node)
            for node in _descendants(module.root, {"export_statement"}):
                if node.child_by_field_name("source") is not None:
                    self._extract_js_reexport(module, node)
            require_calls = {
                _node_key(node)
                for node in _descendants(module.root, {"call_expression"})
                if self._extract_js_require(module, node)
            }
            call_types = {"call_expression"}

        def walk(node: Node, source_id: str, class_id: str | None) -> None:
            """Recursively visits `node` and its children, recording instantiations and calls; see `_extract_relationships` above for the full behavior."""
            source_id, class_id = self._definition_context(module, node, source_id, class_id)
            if node.type in {"new_expression", "object_creation_expression"}:
                constructed = self._add_instantiation(module, node, source_id)
                parent = node.parent
                if constructed is not None and parent is not None and parent.type == "variable_declarator":
                    name_node = parent.child_by_field_name("name")
                    if name_node is not None:
                        module.receiver_types[
                            (source_id, _text(module.source, name_node))
                        ] = constructed.name
            if node.type in call_types and (
                module.language == "java" or _node_key(node) not in require_calls
            ):
                self._add_call(module, node, source_id, class_id)
            for child in node.named_children:
                walk(child, source_id, class_id)

        walk(module.root, module.module_node_id, None)

    @traced("parser.polyglot.relationships")
    def resolve_relationships(self) -> None:
        """Runs the second (relationship) pass across every parsed module: indexes symbols, then extracts imports/calls/instantiations for each.

        No inputs beyond `self`. Calls `_index_symbols()` once (so every
        module's definitions are visible across the whole repository), then
        calls `_extract_relationships` for each module in `self.modules`.
        Called once by `RepositoryAnalyzer.analyze` (backend/app/graph/analyzer.py)
        after all Python and polyglot files have been parsed and their
        definitions indexed; this is the polyglot equivalent of running
        `RelationshipVisitor` over every Python module, and must happen after
        every module's `_extract_definitions` pass has completed.
        """
        self._index_symbols()
        for module in self.modules:
            self._extract_relationships(module)
        log_event(
            logger,
            logging.INFO,
            "parser.polyglot_relationships_completed",
            "JavaScript, TypeScript, and Java relationships extracted",
            module_count=len(self.modules),
            language_counts={
                language: sum(item.language == language for item in self.modules)
                for language in sorted({item.language for item in self.modules})
            },
        )
