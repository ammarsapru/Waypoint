import {
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  AlertTriangle,
  Braces,
  CircleDot,
  FileCode2,
  Github,
  GitFork,
  LoaderCircle,
  Maximize2,
  Minimize2,
  Moon,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  Play,
  RefreshCw,
  Search,
  ShieldCheck,
  Sun,
  Timer,
  UnfoldHorizontal,
} from "lucide-react";
import {
  analyzeGitHubRepository,
  analyzeRepository,
  fetchAnalysis,
  fetchAnalyses,
  fetchIndexStatus,
  fetchRepositoryTree,
  fetchNeighborhood,
  fetchSource,
  rebuildIndex,
} from "./api";
import { FileTree } from "./components/FileTree";
import { AskWorkspace } from "./components/AskWorkspace";
import {
  GraphView,
  type FileCardSize,
  type GraphOrientation,
  type GraphPresentation,
} from "./components/GraphView";
import { Inspector } from "./components/Inspector";
import { IssuesWorkspace } from "./components/IssuesWorkspace";
import { OnboardingWorkspace } from "./components/OnboardingWorkspace";
import { SourcePanel } from "./components/SourcePanel";
import type {
  AnalysisReport,
  AnalysisSessionSummary,
  EdgeKind,
  GraphNode,
  IndexStatus,
  NodeKind,
  RepositoryAlias,
  Selection,
  SourceDocument,
  SourceSpan,
} from "./types";

const nodeKindOptions: NodeKind[] = [
  "repository",
  "module",
  "class",
  "function",
  "method",
];
const edgeKindOptions: EdgeKind[] = [
  "contains",
  "imports",
  "may_call",
  "instantiates",
];
type Theme = "light" | "dark";
type WorkspaceMode = "explore" | "ask" | "onboard" | "issues";
type RepositorySource = "local" | "github";

function initialWorkspaceMode(): WorkspaceMode {
  const stored = localStorage.getItem("waypoint-workspace-mode");
  return stored === "ask" || stored === "onboard" || stored === "issues"
    ? stored
    : "explore";
}

function initialTheme(): Theme {
  const stored = localStorage.getItem("waypoint-theme");
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function selectedSpan(selection: Selection): SourceSpan | null {
  if (!selection) return null;
  return selection.type === "node"
    ? selection.value.span
    : selection.value.evidence.span;
}

function App() {
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [workspaceMode, setWorkspaceMode] =
    useState<WorkspaceMode>(initialWorkspaceMode);
  const [repositorySource, setRepositorySource] =
    useState<RepositorySource>("local");
  const [repositoryPath, setRepositoryPath] = useState(".");
  const [repositoryUrl, setRepositoryUrl] = useState(
    "https://github.com/pallets/flask",
  );
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [recentSessions, setRecentSessions] = useState<AnalysisSessionSummary[]>([]);
  const [selection, setSelection] = useState<Selection>(null);
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(null);
  const [repositoryPaths, setRepositoryPaths] = useState<string[]>([]);
  const [repositoryAliases, setRepositoryAliases] = useState<RepositoryAlias[]>([]);
  const [source, setSource] = useState<SourceDocument | null>(null);
  const [loading, setLoading] = useState(false);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [indexStatus, setIndexStatus] = useState<IndexStatus | null>(null);
  const [indexLoading, setIndexLoading] = useState(false);
  const [repositoryCollapsed, setRepositoryCollapsed] = useState(false);
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  const [graphFullscreen, setGraphFullscreen] = useState(false);
  const [graphPresentation, setGraphPresentation] =
    useState<GraphPresentation>("symbols");
  const [fileCardSize, setFileCardSize] =
    useState<FileCardSize>("comfortable");
  const [graphOrientation, setGraphOrientation] =
    useState<GraphOrientation>("TB");
  const [sourceHeight, setSourceHeight] = useState(300);
  const [expanding, setExpanding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [visibleNodeKinds, setVisibleNodeKinds] = useState<Set<NodeKind>>(
    new Set(["repository", "module"]),
  );
  const [visibleEdgeKinds, setVisibleEdgeKinds] = useState<Set<EdgeKind>>(
    new Set(["contains", "imports", "may_call", "instantiates"]),
  );

  const nodesById = useMemo(
    () => new Map(report?.nodes.map((node) => [node.id, node]) ?? []),
    [report],
  );
  const activeSpan = selectedSpan(selection) ?? (
    selectedFilePath
      ? {
          path: selectedFilePath,
          start_line: 1,
          start_column: 0,
          end_line:
            source?.path === selectedFilePath ? source.line_count : 1,
          end_column: 0,
        }
      : null
  );

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("waypoint-theme", theme);
  }, [theme]);

  useEffect(() => {
    localStorage.setItem("waypoint-workspace-mode", workspaceMode);
  }, [workspaceMode]);

  useEffect(() => {
    const analysisId = localStorage.getItem("waypoint-analysis-id");
    let cancelled = false;
    fetchAnalyses()
      .then(async (sessions) => {
        if (cancelled) return;
        setRecentSessions(sessions);
        const selected = analysisId && sessions.some((item) => item.analysis_id === analysisId)
          ? analysisId
          : null;
        if (selected) setReport(await fetchAnalysis(selected));
      })
      .catch(() => {
        if (analysisId) localStorage.removeItem("waypoint-analysis-id");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const openAnalysisSession = async (analysisId: string) => {
    if (!analysisId || analysisId === report?.analysis_id) return;
    setLoading(true);
    setError(null);
    setSelection(null);
    setSelectedFilePath(null);
    setSource(null);
    try {
      const restored = await fetchAnalysis(analysisId);
      setReport(restored);
      setWorkspaceMode("explore");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Session could not be restored");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (report?.analysis_id) {
      localStorage.setItem("waypoint-analysis-id", report.analysis_id);
    }
  }, [report?.analysis_id]);

  useEffect(() => {
    if (!report?.analysis_id) {
      setRepositoryPaths([]);
      setRepositoryAliases([]);
      return;
    }
    let cancelled = false;
    fetchRepositoryTree(report.analysis_id)
      .then((tree) => {
        if (!cancelled) {
          setRepositoryPaths(tree.paths);
          setRepositoryAliases(tree.aliases);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setRepositoryPaths(
            report.nodes.flatMap((node) =>
              node.kind === "module" && node.span ? [node.span.path] : [],
            ),
          );
          setRepositoryAliases([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [report]);

  useEffect(() => {
    if (!report?.analysis_id) {
      setIndexStatus(null);
      return;
    }
    let cancelled = false;
    fetchIndexStatus(report.analysis_id)
      .then((status) => {
        if (!cancelled) setIndexStatus(status);
      })
      .catch(() => {
        if (!cancelled) setIndexStatus(null);
      });
    return () => {
      cancelled = true;
    };
  }, [report?.analysis_id]);

  useEffect(() => {
    if (!graphFullscreen) return;
    const exitFullscreen = (event: KeyboardEvent) => {
      if (event.key === "Escape") setGraphFullscreen(false);
    };
    window.addEventListener("keydown", exitFullscreen);
    return () => window.removeEventListener("keydown", exitFullscreen);
  }, [graphFullscreen]);

  useEffect(() => {
    if (!report?.analysis_id || !activeSpan) {
      setSource(null);
      setSourceError(null);
      return;
    }
    let cancelled = false;
    setSourceLoading(true);
    setSourceError(null);
    fetchSource(report.analysis_id, activeSpan.path)
      .then((document) => {
        if (!cancelled) setSource(document);
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setSource(null);
          setSourceError(
            caught instanceof Error ? caught.message : "Source request failed",
          );
        }
      })
      .finally(() => {
        if (!cancelled) setSourceLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [report?.analysis_id, activeSpan?.path]);

  const runAnalysis = async () => {
    setLoading(true);
    setError(null);
    setSelection(null);
    setSelectedFilePath(null);
    setSource(null);
    try {
      const nextReport =
        repositorySource === "github"
          ? await analyzeGitHubRepository(repositoryUrl.trim())
          : await analyzeRepository(repositoryPath.trim() || ".");
      setReport(nextReport);
      const sessions = await fetchAnalyses();
      setRecentSessions(sessions);
      setWorkspaceMode("explore");
      const initialNode =
        nextReport.nodes.find((node) => node.kind === "module") ??
        nextReport.nodes.find((node) => node.kind === "repository") ??
        null;
      if (initialNode) setSelection({ type: "node", value: initialNode });
    } catch (caught) {
      setReport(null);
      setError(
        caught instanceof Error ? caught.message : "Repository analysis failed",
      );
    } finally {
      setLoading(false);
    }
  };

  const rebuildCurrentIndex = async () => {
    if (!report?.analysis_id || indexLoading) return;
    setIndexLoading(true);
    setError(null);
    try {
      const status = await rebuildIndex(report.analysis_id);
      const refreshed = await fetchAnalysis(report.analysis_id);
      setIndexStatus(status);
      setReport(refreshed);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Index rebuild failed");
    } finally {
      setIndexLoading(false);
    }
  };

  const toggleNodeKind = (kind: NodeKind) => {
    setVisibleNodeKinds((current) => {
      const next = new Set(current);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  };

  const toggleEdgeKind = (kind: EdgeKind) => {
    setVisibleEdgeKinds((current) => {
      const next = new Set(current);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  };

  const handleFileSelect = (node: GraphNode) => {
    setSelectedFilePath(null);
    setSelection({ type: "node", value: node });
    if (!visibleNodeKinds.has("module")) {
      setVisibleNodeKinds((current) => new Set([...current, "module"]));
    }
  };

  const handleTreeFileSelect = (path: string, node?: GraphNode) => {
    if (node) {
      handleFileSelect(node);
      return;
    }
    setSelection(null);
    setSelectedFilePath(path);
  };

  const expandNode = async (node: GraphNode) => {
    if (!report?.analysis_id || expanding) return;
    setExpanding(true);
    setError(null);
    try {
      const neighborhood = await fetchNeighborhood(
        report.analysis_id,
        node.id,
        1,
      );
      setReport((current) => {
        if (!current) return current;
        const nodes = new Map(current.nodes.map((item) => [item.id, item]));
        const edges = new Map(current.edges.map((item) => [item.id, item]));
        neighborhood.nodes.forEach((item) => nodes.set(item.id, item));
        neighborhood.edges.forEach((item) => edges.set(item.id, item));
        return {
          ...current,
          nodes: [...nodes.values()],
          edges: [...edges.values()],
        };
      });
      setVisibleNodeKinds(
        new Set(["repository", "module", "class", "function", "method"]),
      );
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not expand graph neighborhood",
      );
    } finally {
      setExpanding(false);
    }
  };

  const openNodeFromWorkspace = (nodeId: string) => {
    const node = report?.nodes.find((item) => item.id === nodeId);
    if (node) {
      setSelection({ type: "node", value: node });
      setWorkspaceMode("explore");
    }
  };

  const sourceHeightWithinStage = (
    stage: HTMLElement,
    requestedHeight: number,
  ) => {
    const available = stage.getBoundingClientRect().height - 45 - 8;
    return Math.round(
      Math.min(Math.max(120, requestedHeight), Math.max(120, available - 160)),
    );
  };

  const beginSourceResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (graphFullscreen) return;
    event.preventDefault();
    const stage = event.currentTarget.parentElement;
    if (!stage) return;
    const startY = event.clientY;
    const startHeight = sourceHeight;
    document.body.classList.add("resizing-source");
    const resize = (moveEvent: PointerEvent) => {
      setSourceHeight(
        sourceHeightWithinStage(
          stage,
          startHeight + startY - moveEvent.clientY,
        ),
      );
    };
    const finish = () => {
      document.body.classList.remove("resizing-source");
      window.removeEventListener("pointermove", resize);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
    };
    window.addEventListener("pointermove", resize);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
  };

  const resizeSourceWithKeyboard = (
    event: ReactKeyboardEvent<HTMLDivElement>,
  ) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    const stage = event.currentTarget.parentElement;
    if (!stage) return;
    const delta = event.key === "ArrowUp" ? 24 : -24;
    setSourceHeight((current) =>
      sourceHeightWithinStage(stage, current + delta),
    );
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">
            <Network size={20} />
          </div>
          <div>
            <strong>Waypoint</strong>
            <span>Codebase navigator</span>
          </div>
        </div>
        <div className="analysis-form">
          <label htmlFor="repository-source">Source</label>
          <select
            id="repository-source"
            aria-label="Repository source"
            value={repositorySource}
            onChange={(event) =>
              setRepositorySource(event.target.value as RepositorySource)
            }
            disabled={loading}
          >
            <option value="local">Local path</option>
            <option value="github">GitHub repository</option>
          </select>
          <input
            id="repository-path"
            aria-label={
              repositorySource === "github"
                ? "GitHub repository URL"
                : "Local repository path"
            }
            value={
              repositorySource === "github" ? repositoryUrl : repositoryPath
            }
            onChange={(event) =>
              repositorySource === "github"
                ? setRepositoryUrl(event.target.value)
                : setRepositoryPath(event.target.value)
            }
            onKeyDown={(event) => {
              if (event.key === "Enter" && !loading) void runAnalysis();
            }}
            placeholder={
              repositorySource === "github"
                ? "https://github.com/owner/repository"
                : "Path inside the allowed root"
            }
          />
          <button
            type="button"
            className="primary-action"
            onClick={() => void runAnalysis()}
            disabled={loading}
          >
            {loading ? (
              <LoaderCircle className="spin" size={17} />
            ) : repositorySource === "github" ? (
              <Github size={16} />
            ) : (
              <Play size={16} />
            )}
            {loading
              ? repositorySource === "github"
                ? "Cloning & mapping…"
                : "Mapping…"
              : "Analyze"}
          </button>
        </div>
        <div className="topbar-actions">
          {recentSessions.length > 0 && (
            <label className="recent-session-picker">
              <span>Recent</span>
              <select
                aria-label="Open a previous repository session"
                value={report?.analysis_id ?? ""}
                disabled={loading}
                onChange={(event) => void openAnalysisSession(event.target.value)}
              >
                {!report && <option value="">Choose repository</option>}
                {recentSessions.map((session) => (
                  <option key={session.analysis_id} value={session.analysis_id}>
                    {session.repository_name} · {session.files_parsed} files · {session.analysis_id.slice(0, 8)}
                  </option>
                ))}
              </select>
            </label>
          )}
          {report && (
            <nav className="workspace-nav" aria-label="Workspace">
              {(["explore", "ask", "onboard", "issues"] as WorkspaceMode[]).map(
                (mode) => (
                  <button
                    type="button"
                    key={mode}
                    className={workspaceMode === mode ? "active" : ""}
                    onClick={() => setWorkspaceMode(mode)}
                  >
                    {mode}
                  </button>
                ),
              )}
            </nav>
          )}
          <div className="trust-label">
            <ShieldCheck size={16} />
            Static analysis · no repository execution
          </div>
          <button
            type="button"
            className="theme-toggle inline-flex size-9 items-center justify-center rounded-md border transition-colors"
            onClick={() =>
              setTheme((current) => (current === "dark" ? "light" : "dark"))
            }
            aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
            title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
          >
            {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
          </button>
        </div>
      </header>

      {report ? (
        <>
          <section className="metrics-bar">
            <div>
              <FileCode2 size={16} />
              <strong>{repositoryPaths.length || report.stats.files_parsed}</strong>
              <span>indexed files</span>
            </div>
            <div>
              <CircleDot size={16} />
              <strong>{report.stats.node_count}</strong>
              <span>nodes</span>
            </div>
            <div>
              <GitFork size={16} />
              <strong>{report.stats.edge_count}</strong>
              <span>edges</span>
            </div>
            <div>
              <AlertTriangle size={16} />
              <strong>{report.stats.unresolved_count}</strong>
              <span>unresolved</span>
            </div>
            <div>
              <Timer size={16} />
              <strong>{Math.round(report.stats.duration_ms)}</strong>
              <span>ms</span>
            </div>
            <button
              type="button"
              className="index-status-metric"
              onClick={rebuildCurrentIndex}
              disabled={indexLoading}
              title={
                indexStatus
                  ? `Revision ${indexStatus.revision_id}. ${indexStatus.chunks} chunks, ${indexStatus.vectors} vectors, ${indexStatus.aliases ?? 0} aliases. Click to rebuild.`
                  : "Load or rebuild the repository index"
              }
            >
              <RefreshCw size={16} className={indexLoading ? "spin" : ""} />
              <strong>{indexStatus?.chunks ?? "—"}</strong>
              <span>{indexStatus?.status === "complete" ? "chunks" : "index"}</span>
            </button>
            <div className="analysis-identity">
              <span>{report.repository_name}</span>
              <code>{report.analysis_id.slice(0, 10)}</code>
            </div>
          </section>

          {workspaceMode === "explore" ? (
          <main
            className={[
              "workspace",
              repositoryCollapsed ? "repository-collapsed" : "",
              inspectorCollapsed ? "inspector-collapsed" : "",
            ].join(" ")}
          >
            <aside className={`sidebar ${repositoryCollapsed ? "collapsed" : ""}`}>
              <div className="panel-heading">
                <div className="panel-title">
                  <span>Repository</span>
                  <small>
                    {repositoryPaths.length} entries · {report.stats.files_parsed} parsed
                    {repositoryAliases.length ? ` · ${repositoryAliases.length} aliases` : ""}
                  </small>
                </div>
                <button
                  type="button"
                  className="panel-collapse"
                  aria-label={
                    repositoryCollapsed
                      ? "Expand repository sidebar"
                      : "Collapse repository sidebar"
                  }
                  title={
                    repositoryCollapsed
                      ? "Expand repository sidebar"
                      : "Collapse repository sidebar"
                  }
                  onClick={() => setRepositoryCollapsed((current) => !current)}
                >
                  {repositoryCollapsed ? (
                    <PanelLeftOpen size={16} />
                  ) : (
                    <PanelLeftClose size={16} />
                  )}
                </button>
              </div>
              {!repositoryCollapsed && (
                <FileTree
                  nodes={report.nodes}
                  paths={repositoryPaths}
                  aliases={repositoryAliases}
                  selectedPath={activeSpan?.path}
                  onSelect={handleTreeFileSelect}
                />
              )}
            </aside>

            <section
              className={`center-stage ${graphFullscreen ? "graph-fullscreen" : ""}`}
              style={
                {
                  "--source-height": `${sourceHeight}px`,
                } as CSSProperties
              }
            >
              <div className="graph-toolbar">
                <div className="filter-group graph-presentation-toggle">
                  <span>View</span>
                  <button
                    type="button"
                    className={graphPresentation === "symbols" ? "active" : ""}
                    aria-pressed={graphPresentation === "symbols"}
                    onClick={() => setGraphPresentation("symbols")}
                  >
                    Symbols
                  </button>
                  <button
                    type="button"
                    className={graphPresentation === "files" ? "active" : ""}
                    aria-pressed={graphPresentation === "files"}
                    onClick={() => setGraphPresentation("files")}
                  >
                    File cards
                  </button>
                </div>
                <div className="filter-group graph-orientation-toggle">
                  <span>Layout</span>
                  <button
                    type="button"
                    className={graphOrientation === "TB" ? "active" : ""}
                    aria-pressed={graphOrientation === "TB"}
                    title="Hierarchy flows top to bottom, siblings spread left-right"
                    onClick={() => setGraphOrientation("TB")}
                  >
                    Top-down
                  </button>
                  <button
                    type="button"
                    className={graphOrientation === "LR" ? "active" : ""}
                    aria-pressed={graphOrientation === "LR"}
                    title="Hierarchy flows left to right, siblings spread top-bottom"
                    onClick={() => setGraphOrientation("LR")}
                  >
                    Left-right
                  </button>
                </div>
                {graphPresentation === "files" && (
                  <div className="filter-group file-card-size-toggle">
                    <span>Card size</span>
                    {(["compact", "comfortable", "large"] as FileCardSize[]).map(
                      (size) => (
                        <button
                          type="button"
                          key={size}
                          className={fileCardSize === size ? "active" : ""}
                          aria-pressed={fileCardSize === size}
                          onClick={() => setFileCardSize(size)}
                        >
                          {size === "comfortable" ? "Medium" : size[0].toUpperCase() + size.slice(1)}
                        </button>
                      ),
                    )}
                  </div>
                )}
                <div className="search-box">
                  <Search size={15} />
                  <input
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    placeholder="Filter symbols"
                  />
                </div>
                <div className="filter-group">
                  <span>Nodes</span>
                  {nodeKindOptions.map((kind) => (
                    <button
                      type="button"
                      key={kind}
                      className={visibleNodeKinds.has(kind) ? "active" : ""}
                      onClick={() => toggleNodeKind(kind)}
                    >
                      {kind}
                    </button>
                  ))}
                </div>
                {selection?.type === "node" && (
                  <button
                    type="button"
                    className="expand-action"
                    onClick={() => void expandNode(selection.value)}
                    disabled={expanding}
                  >
                    {expanding ? (
                      <LoaderCircle className="spin" size={13} />
                    ) : (
                      <UnfoldHorizontal size={13} />
                    )}
                    Expand
                  </button>
                )}
                <button
                  type="button"
                  className="fullscreen-action"
                  aria-label={
                    graphFullscreen
                      ? "Exit graph fullscreen"
                      : "Open graph fullscreen"
                  }
                  title={
                    graphFullscreen
                      ? "Exit graph fullscreen (Esc)"
                      : "Open graph fullscreen"
                  }
                  aria-pressed={graphFullscreen}
                  onClick={() => setGraphFullscreen((current) => !current)}
                >
                  {graphFullscreen ? (
                    <Minimize2 size={14} />
                  ) : (
                    <Maximize2 size={14} />
                  )}
                  {graphFullscreen ? "Exit fullscreen" : "Fullscreen"}
                </button>
                <div className="filter-group">
                  <span>Edges</span>
                  {edgeKindOptions.map((kind) => (
                    <button
                      type="button"
                      key={kind}
                      className={visibleEdgeKinds.has(kind) ? "active" : ""}
                      onClick={() => toggleEdgeKind(kind)}
                    >
                      {kind.replace("_", " ")}
                    </button>
                  ))}
                </div>
              </div>

              <div className="graph-pane">
                <GraphView
                  key={graphFullscreen ? "fullscreen" : "embedded"}
                  nodes={report.nodes}
                  edges={report.edges}
                  unresolvedReferences={report.unresolved_references}
                  visibleNodeKinds={visibleNodeKinds}
                  visibleEdgeKinds={visibleEdgeKinds}
                  search={search}
                  selection={selection}
                  onSelect={(nextSelection) => {
                    setSelectedFilePath(null);
                    setSelection(nextSelection);
                  }}
                  onExpand={(node) => void expandNode(node)}
                  theme={theme}
                  presentation={graphPresentation}
                  fileCardSize={fileCardSize}
                  orientation={graphOrientation}
                />
              </div>

              <div
                className="source-resizer"
                role="separator"
                aria-label="Resize source viewer"
                aria-orientation="horizontal"
                aria-valuemin={120}
                aria-valuemax={700}
                aria-valuenow={sourceHeight}
                tabIndex={0}
                onPointerDown={beginSourceResize}
                onKeyDown={resizeSourceWithKeyboard}
                onDoubleClick={() => setSourceHeight(300)}
              >
                <span />
              </div>

              <div className="source-pane">
                <div className="source-heading">
                  <div>
                    <Braces size={15} />
                    <strong>{source?.path ?? "Source viewer"}</strong>
                  </div>
                  {activeSpan && (
                    <span>
                      L{activeSpan.start_line}–{activeSpan.end_line}
                    </span>
                  )}
                </div>
                <SourcePanel
                  source={source}
                  span={activeSpan}
                  loading={sourceLoading}
                  error={sourceError}
                  theme={theme}
                />
              </div>
            </section>

            <aside className={`inspector ${inspectorCollapsed ? "collapsed" : ""}`}>
              <div className="panel-heading">
                <div className="panel-title">
                  <span>Inspector</span>
                  <small>Evidence and identity</small>
                </div>
                <button
                  type="button"
                  className="panel-collapse"
                  aria-label={
                    inspectorCollapsed
                      ? "Expand inspector sidebar"
                      : "Collapse inspector sidebar"
                  }
                  title={
                    inspectorCollapsed
                      ? "Expand inspector sidebar"
                      : "Collapse inspector sidebar"
                  }
                  onClick={() => setInspectorCollapsed((current) => !current)}
                >
                  {inspectorCollapsed ? (
                    <PanelRightOpen size={16} />
                  ) : (
                    <PanelRightClose size={16} />
                  )}
                </button>
              </div>
              {!inspectorCollapsed && (
                <Inspector
                  selection={selection}
                  nodesById={nodesById}
                  analysisId={report.analysis_id}
                  onOpenNode={openNodeFromWorkspace}
                />
              )}
            </aside>
          </main>
          ) : workspaceMode === "ask" ? (
            <AskWorkspace
              report={report}
              onOpenNode={openNodeFromWorkspace}
              theme={theme}
            />
          ) : workspaceMode === "onboard" ? (
            <OnboardingWorkspace
              report={report}
              onOpenNode={openNodeFromWorkspace}
              theme={theme}
            />
          ) : (
            <IssuesWorkspace report={report} />
          )}
        </>
      ) : (
        <main className="welcome">
          <div className="welcome-copy">
            <div className="eyebrow">Evidence-aware repository understanding</div>
            <h1>See the structure.<br />Inspect the proof.</h1>
            <p>
              Analyze a Python, JavaScript, TypeScript, or Java codebase into
              verified structure, inferred call paths, and source-backed evidence.
              No repository code is executed.
            </p>
            <button
              type="button"
              className="welcome-action"
              onClick={() => void runAnalysis()}
              disabled={loading}
            >
              {loading ? (
                <LoaderCircle className="spin" size={18} />
              ) : (
                <Network size={18} />
              )}
              {repositorySource === "github"
                ? "Clone and map GitHub repository"
                : "Map the current repository"}
            </button>
            {error && <div className="error-banner">{error}</div>}
          </div>
          <div className="welcome-diagram" aria-hidden="true">
            <svg className="diagram-links" viewBox="0 0 100 100" preserveAspectRatio="none">
              <path d="M50 19 L18 40" />
              <path d="M50 19 L82 40" />
              <path d="M18 49 L24 78" />
              <path d="M82 49 L76 78" />
            </svg>
            <div className="diagram-node root">repository</div>
            <div className="diagram-node module-a">api.routes</div>
            <div className="diagram-node module-b">graph.analyzer</div>
            <div className="diagram-node method-a">analyze()</div>
            <div className="diagram-node method-b">resolve()</div>
          </div>
        </main>
      )}
    </div>
  );
}

export default App;
