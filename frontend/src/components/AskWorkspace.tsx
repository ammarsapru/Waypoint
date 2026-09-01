import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowUp,
  Bot,
  Braces,
  ExternalLink,
  FileCode2,
  FileText,
  GitCompareArrows,
  LoaderCircle,
  MessageSquareText,
  ShieldAlert,
  Sparkles,
} from "lucide-react";
import {
  askRepository,
  compareAgenticRetrieval,
  fetchLatestConversation,
  fetchSource,
} from "../api";
import type {
  AgenticComparisonReport,
  AnalysisReport,
  GroundedAnswer,
  GroundedCitation,
  SourceDocument,
} from "../types";
import { SourcePanel } from "./SourcePanel";
import { LazyMarkdownContent } from "./LazyMarkdownContent";

interface AskWorkspaceProps {
  report: AnalysisReport;
  onOpenNode: (nodeId: string) => void;
  theme: "light" | "dark";
}

interface ChatTurn {
  id: string;
  question: string;
  answer: GroundedAnswer | null;
}

interface EvidenceFile {
  path: string;
  citations: GroundedCitation[];
}

export function AskWorkspace({
  report,
  onOpenNode,
  theme,
}: AskWorkspaceProps) {
  const [question, setQuestion] = useState(
    "What is this repository about? Highlight its top 10 features.",
  );
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [comparisonLoading, setComparisonLoading] = useState(false);
  const [comparison, setComparison] = useState<AgenticComparisonReport | null>(null);
  const [expectedPaths, setExpectedPaths] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [selectedCitation, setSelectedCitation] =
    useState<GroundedCitation | null>(null);
  const [source, setSource] = useState<SourceDocument | null>(null);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const threadRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    setTurns([]);
    setConversationId(null);
    setSelectedCitation(null);
    setError(null);
    fetchLatestConversation(report.analysis_id)
      .then((transcript) => {
        if (cancelled) return;
        setConversationId((current) => current ?? transcript.conversation_id);
        const restoredTurns = transcript.turns.map((turn) => ({
            id: crypto.randomUUID(),
            question: turn.question,
            answer: turn.answer,
          }));
        setTurns((current) => current.length ? current : restoredTurns);
        const latest = transcript.turns.at(-1)?.answer;
        setSelectedCitation((current) => current ?? latest?.citations[0] ?? null);
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setError(
            caught instanceof Error
              ? caught.message
              : "Conversation history could not be restored",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [report.analysis_id]);

  // Only the LATEST turn's answer counts as "active" - falling back to an
  // earlier turn with an answer (as this used to) means the evidence panel
  // keeps showing the previous question's citations while a new question is
  // still loading, which reads as evidence for the new question.
  const latestTurn = turns.length ? turns[turns.length - 1] : null;
  const activeAnswer = latestTurn?.answer ?? null;
  const evidenceFiles = useMemo<EvidenceFile[]>(() => {
    const grouped = new Map<string, GroundedCitation[]>();
    for (const citation of activeAnswer?.citations ?? []) {
      const existing = grouped.get(citation.span.path) ?? [];
      existing.push(citation);
      grouped.set(citation.span.path, existing);
    }
    return [...grouped.entries()].map(([path, citations]) => ({
      path,
      citations,
    }));
  }, [activeAnswer]);

  useEffect(() => {
    threadRef.current?.scrollTo({
      top: threadRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [turns, loading]);

  useEffect(() => {
    if (!selectedCitation) {
      setSource(null);
      setSourceError(null);
      return;
    }
    let cancelled = false;
    setSourceLoading(true);
    setSourceError(null);
    fetchSource(report.analysis_id, selectedCitation.span.path)
      .then((document) => {
        if (!cancelled) setSource(document);
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setSource(null);
          setSourceError(
            caught instanceof Error ? caught.message : "Evidence file failed",
          );
        }
      })
      .finally(() => {
        if (!cancelled) setSourceLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [report.analysis_id, selectedCitation]);

  const chooseCitation = (citation: GroundedCitation) => {
    setSelectedCitation(citation);
  };

  const chooseFile = (file: EvidenceFile) => {
    chooseCitation(file.citations[0]);
  };

  const ask = async (prompt = question) => {
    const normalized = prompt.trim();
    if (normalized.length < 2 || loading) return;
    const turnId = crypto.randomUUID();
    setQuestion("");
    setTurns((current) => [
      ...current,
      { id: turnId, question: normalized, answer: null },
    ]);
    setLoading(true);
    setError(null);
    try {
      const answer = await askRepository(
        report.analysis_id,
        normalized,
        [],
        conversationId,
      );
      setConversationId(answer.conversation_id ?? conversationId);
      setTurns((current) =>
        current.map((turn) =>
          turn.id === turnId ? { ...turn, answer } : turn,
        ),
      );
      setSelectedCitation(answer.citations[0] ?? null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Question failed");
    } finally {
      setLoading(false);
    }
  };

  const compareAgents = async () => {
    const prompt = question.trim() || turns.at(-1)?.question || "";
    if (prompt.length < 2 || comparisonLoading) return;
    setComparisonLoading(true);
    setError(null);
    try {
      const paths = expectedPaths
        .split(/[\n,]/)
        .map((path) => path.trim())
        .filter(Boolean);
      setComparison(
        await compareAgenticRetrieval(report.analysis_id, prompt, paths),
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Comparison failed");
    } finally {
      setComparisonLoading(false);
    }
  };

  const selectFeatureEvidence = (
    answer: GroundedAnswer,
    sourcePath: string,
    sourceLine: number,
  ) => {
    const citation = answer.citations.find(
      (item) =>
        item.span.path === sourcePath &&
        item.span.start_line === sourceLine,
    );
    if (citation) chooseCitation(citation);
  };

  return (
    <main className="ask-split-workspace">
      <section className="ask-chat-panel">
        <header className="ask-chat-header">
          <div className="ask-agent-mark">
            <Sparkles size={17} />
          </div>
          <div>
            <strong>Ask Waypoint</strong>
            <span>Answers must cite repository evidence</span>
          </div>
          <small>{report.repository_name}</small>
          <button
            type="button"
            className="agent-compare-button"
            disabled={comparisonLoading || (!question.trim() && !turns.length)}
            onClick={() => void compareAgents()}
            title="Each model independently chooses and calls repository tools"
          >
            {comparisonLoading ? (
              <LoaderCircle className="spin" size={14} />
            ) : (
              <GitCompareArrows size={14} />
            )}
            Compare agents
          </button>
        </header>

        <div className="chat-thread" ref={threadRef}>
          {!turns.length && (
            <div className="chat-welcome">
              <MessageSquareText size={28} />
              <h1>Understand the repository through its evidence.</h1>
              <p>
                Ask for a product overview, major features, architecture,
                ownership, or where a behavior is implemented. Supporting files
                will open beside the answer.
              </p>
              <div className="prompt-starters">
                {[
                  "What is this repository about? Highlight its top 10 features.",
                  "Which files are the main application entry points?",
                  "How is the backend organized?",
                ].map((prompt) => (
                  <button
                    type="button"
                    key={prompt}
                    onClick={() => void ask(prompt)}
                  >
                    {prompt}
                  </button>
                ))}
              </div>
            </div>
          )}

          {turns.map((turn) => (
            <div className="chat-exchange" key={turn.id}>
              <div className="chat-message user-message">
                <span>You</span>
                <p>{turn.question}</p>
              </div>
              {turn.answer && <div className="chat-message assistant-message">
                <div className="assistant-identity">
                  {turn.answer.refused ? (
                    <ShieldAlert size={16} />
                  ) : (
                    <Bot size={16} />
                  )}
                  <span>Waypoint</span>
                  <small>{turn.answer.provider.replace("-", " ")}</small>
                </div>
                {turn.answer.summary ? (
                  <LazyMarkdownContent
                    className="answer-summary"
                    content={turn.answer.summary}
                  />
                ) : (
                  <LazyMarkdownContent
                    className={turn.answer.refused ? "refusal" : ""}
                    content={turn.answer.answer}
                  />
                )}
                {turn.answer.features.length > 0 && (
                  <ol className="feature-answer-list">
                    {turn.answer.features.map((feature) => (
                      <li key={`${feature.title}-${feature.source_line}`}>
                        <button
                          type="button"
                          onClick={() =>
                            selectFeatureEvidence(
                              turn.answer!,
                              feature.source_path,
                              feature.source_line,
                            )
                          }
                        >
                          <strong>{feature.title}</strong>
                          <span>{feature.description}</span>
                          <small>
                            {feature.source_path}:L{feature.source_line}
                          </small>
                        </button>
                      </li>
                    ))}
                  </ol>
                )}
                <div className="answer-grounding">
                  <FileText size={13} />
                  <span>
                    {turn.answer.citations.length} cited passages ·{" "}
                    {new Set(
                      turn.answer.citations.map(
                        (citation) => citation.span.path,
                      ),
                    ).size}{" "}
                    files
                    {turn.answer.inspected_file_count > 0
                      ? ` · ${turn.answer.inspected_file_count} inspected`
                      : ""}
                  </span>
                </div>
              </div>}
            </div>
          ))}

          {loading && (
            <div className="chat-thinking">
              <LoaderCircle className="spin" size={17} />
              Reading documentation and graph evidence…
            </div>
          )}
          {error && <div className="error-banner">{error}</div>}
        </div>

        <div className="chat-composer">
          <textarea
            aria-label="Repository question"
            value={question}
            rows={3}
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void ask();
              }
            }}
            placeholder="Ask about this repository…"
          />
          <button
            type="button"
            aria-label="Send repository question"
            disabled={loading || question.trim().length < 2}
            onClick={() => void ask()}
          >
            {loading ? (
              <LoaderCircle className="spin" size={17} />
            ) : (
              <ArrowUp size={17} />
            )}
          </button>
          <small>Enter to send · Shift+Enter for a new line</small>
          <input
            aria-label="Expected repository paths for retrieval scoring"
            value={expectedPaths}
            onChange={(event) => setExpectedPaths(event.target.value)}
            placeholder="Optional expected paths, comma-separated"
          />
        </div>
      </section>

      <section
        className={`ask-evidence-panel ${
          activeAnswer?.tool_trace?.length ? "has-agent-activity" : ""
        }`}
      >
        <header className="evidence-header">
          <div>
            <Braces size={16} />
            <strong>Answer evidence</strong>
          </div>
          <span>
            {evidenceFiles.length
              ? `${evidenceFiles.length} supporting files`
              : "No answer selected"}
          </span>
        </header>

        {!!activeAnswer?.tool_trace?.length && (
          <div className="agent-activity" aria-label="Agent activity">
            <div>
              <strong>Agent activity</strong>
              <span>{activeAnswer.tool_trace.length} tools used</span>
            </div>
            <ol>
              {(activeAnswer.tool_trace ?? []).map((activity, index) => (
                <li key={`${activity.round}-${activity.tool}-${index}`}>
                  <span>{activity.round}</span>
                  <div>
                    <strong>{activity.tool.replaceAll("_", " ")}</strong>
                    <small>
                      {activity.status} · {activity.duration_ms.toFixed(1)} ms ·{" "}
                      {activity.evidence_files.length} evidence files
                    </small>
                  </div>
                </li>
              ))}
            </ol>
          </div>
        )}

        {comparison && (
          <div className="agent-comparison" aria-label="Independent agent comparison">
            <header>
              <strong>Independent retrieval comparison</strong>
              <span>
                {comparison.labels.acceptable_paths.length
                  ? "Scored against supplied expected paths"
                  : "Trajectory-only: add expected paths for objective scores"}
              </span>
            </header>
            <div className="agent-comparison-grid">
              {comparison.runs.map((run) => (
                <article key={`${run.provider}:${run.model}`}>
                  <strong>{run.provider}:{run.model}</strong>
                  {!!run.actual_models.length && (
                    <small>Actual: {run.actual_models.join(", ")}</small>
                  )}
                  <small>
                    {run.repository_tool_calls} tools · {run.total_model_rounds} rounds · {(run.duration_ms / 1000).toFixed(2)}s
                  </small>
                  <dl>
                    <div><dt>Hit@5</dt><dd>{(run.candidate_retrieval.hit_at_5 * 100).toFixed(0)}%</dd></div>
                    <div><dt>MRR</dt><dd>{run.candidate_retrieval.mrr.toFixed(3)}</dd></div>
                    <div><dt>nDCG@10</dt><dd>{run.candidate_retrieval.ndcg_at_10.toFixed(3)}</dd></div>
                    <div><dt>Tokens</dt><dd>{run.total_tokens ?? "n/a"}</dd></div>
                  </dl>
                  <ol>
                    {run.candidate_retrieval.ranked_paths.slice(0, 10).map((path) => (
                      <li key={path}>{path}</li>
                    ))}
                  </ol>
                </article>
              ))}
            </div>
            {comparison.failures.map((failure) => (
              <div className="error-banner" key={`${failure.provider}:${failure.model}`}>
                {failure.provider}:{failure.model} — {failure.detail}
              </div>
            ))}
          </div>
        )}

        {evidenceFiles.length ? (
          <div className="evidence-workspace">
            <aside className="evidence-file-list" aria-label="Answer files">
              {evidenceFiles.map((file) => {
                const active =
                  selectedCitation?.span.path === file.path;
                const primary = file.citations[0];
                return (
                  <button
                    type="button"
                    key={file.path}
                    className={active ? "active" : ""}
                    onClick={() => chooseFile(file)}
                  >
                    <FileCode2 size={15} />
                    <span>
                      <strong>{file.path.split("/").at(-1)}</strong>
                      <small>{file.path}</small>
                      <em>
                        {file.citations.length} cited{" "}
                        {file.citations.length === 1 ? "passage" : "passages"}
                      </em>
                    </span>
                    {primary.node_id && (
                      <ExternalLink
                        size={13}
                        aria-label={`Open ${file.path} in graph`}
                        onClick={(event) => {
                          event.stopPropagation();
                          onOpenNode(primary.node_id as string);
                        }}
                      />
                    )}
                  </button>
                );
              })}
            </aside>

            <div className="evidence-viewer">
              <div className="evidence-context">
                <div>
                  <strong>
                    {selectedCitation?.title ||
                      selectedCitation?.qualified_name}
                  </strong>
                  <span>{selectedCitation?.relevance}</span>
                </div>
                {selectedCitation && (
                  <code>
                    {selectedCitation.span.path}:L
                    {selectedCitation.span.start_line}
                  </code>
                )}
              </div>
              <div className="evidence-source">
                <SourcePanel
                  source={source}
                  span={selectedCitation?.span ?? null}
                  loading={sourceLoading}
                  error={sourceError}
                  theme={theme}
                />
              </div>
              {selectedCitation?.excerpt && (
                <blockquote>{selectedCitation.excerpt}</blockquote>
              )}
            </div>
          </div>
        ) : loading ? (
          <div className="evidence-empty">
            <LoaderCircle className="spin" size={28} />
            <h2>Gathering evidence…</h2>
            <p>Supporting files for this question will appear here shortly.</p>
          </div>
        ) : (
          <div className="evidence-empty">
            <FileText size={28} />
            <h2>Supporting files will appear here.</h2>
            <p>
              Ask a question to see every document and source file used for the
              response, then select a file to inspect its cited lines.
            </p>
          </div>
        )}
      </section>
    </main>
  );
}
