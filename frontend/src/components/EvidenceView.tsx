import { useEffect, useState } from "react";
import { AlertTriangle, ArrowLeft, LoaderCircle } from "lucide-react";
import { fetchIssueInvestigation, fetchIssues, fetchSource } from "../api";
import { LazyMarkdownContent } from "./LazyMarkdownContent";
import type { ProposedIssue } from "../types";

interface EvidenceFileExcerpt {
  path: string;
  startLine: number;
  endLine: number;
  lines: string[];
  error?: string;
}

/**
 * In-app view opened from "Inspect evidence" (see IssuesWorkspace.tsx's
 * ProposalCard). Rendered in place of the issues list/detail panes - IssuesWorkspace
 * stays mounted underneath, so its own state (selected issue, timeline, active tab)
 * is untouched and reappears as-is when the user hits the back arrow.
 */
export function EvidenceView({
  analysisId,
  findingId,
  issueNumber,
  onBack,
}: {
  analysisId: string;
  findingId?: string;
  issueNumber?: number;
  onBack: () => void;
}) {
  const [finding, setFinding] = useState<ProposedIssue | null>(null);
  const [excerpts, setExcerpts] = useState<EvidenceFileExcerpt[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    (async () => {
      let resolved: ProposedIssue | null = null;
      try {
        if (issueNumber !== undefined) {
          resolved = await fetchIssueInvestigation(analysisId, issueNumber);
        } else if (findingId) {
          const workspace = await fetchIssues(analysisId);
          resolved = workspace.proposed_issues.find((item) => item.id === findingId) ?? null;
        }
      } catch (caught) {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : "Failed to load this finding");
          setLoading(false);
        }
        return;
      }
      if (cancelled) return;
      if (!resolved) {
        setError("This finding could not be found - it may have been regenerated since this tab was opened.");
        setLoading(false);
        return;
      }
      setFinding(resolved);

      const loaded = await Promise.all(
        resolved.evidence.map(async (span): Promise<EvidenceFileExcerpt> => {
          try {
            const document = await fetchSource(analysisId, span.path);
            const allLines = document.content.split("\n");
            const startIdx = Math.max(0, span.start_line - 1);
            const endIdx = Math.min(allLines.length, span.end_line);
            return {
              path: span.path,
              startLine: span.start_line,
              endLine: span.end_line,
              lines: allLines.slice(startIdx, endIdx),
            };
          } catch (caught) {
            return {
              path: span.path,
              startLine: span.start_line,
              endLine: span.end_line,
              lines: [],
              error: caught instanceof Error ? caught.message : "Source failed to load",
            };
          }
        }),
      );
      if (!cancelled) {
        setExcerpts(loaded);
        setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [analysisId, findingId, issueNumber]);

  if (loading) {
    return (
      <div className="evidence-view">
        <button type="button" className="evidence-view-back" onClick={onBack}>
          <ArrowLeft size={14} /> Back
        </button>
        <div className="insights-state">
          <LoaderCircle className="spin" size={24} />
          Loading evidence…
        </div>
      </div>
    );
  }

  if (error || !finding) {
    return (
      <div className="evidence-view">
        <button type="button" className="evidence-view-back" onClick={onBack}>
          <ArrowLeft size={14} /> Back
        </button>
        <div className="insights-state error">
          <AlertTriangle size={20} />
          {error ?? "This finding could not be found."}
        </div>
      </div>
    );
  }

  return (
    <main className="evidence-view">
      <button type="button" className="evidence-view-back" onClick={onBack}>
        <ArrowLeft size={14} /> Back
      </button>
      <header className="evidence-view-header">
        <div className="proposal-heading">
          <span className={`finding-source-badge finding-source-${finding.source}`}>
            {finding.source.replace("_", " ")}
          </span>
          {finding.verdict && (
            <span className={`finding-verdict-badge finding-verdict-${finding.verdict}`}>
              {finding.verdict.replace("_", " ")}
            </span>
          )}
          <em>{Math.round(finding.confidence * 100)}% confidence</em>
        </div>
        <h1>{finding.title}</h1>
        <p>{finding.explanation}</p>
      </header>

      <div className="evidence-view-split">
        <section className="evidence-view-pane">
          <h2>Evidence ({excerpts.length} file{excerpts.length === 1 ? "" : "s"})</h2>
          {excerpts.map((excerpt) => (
            <article key={`${excerpt.path}:${excerpt.startLine}`} className="evidence-file-excerpt">
              <h3>{excerpt.path}:L{excerpt.startLine}-L{excerpt.endLine}</h3>
              {excerpt.error ? (
                <div className="issue-warning"><AlertTriangle size={14} />{excerpt.error}</div>
              ) : (
                <pre><code>
                  {excerpt.lines
                    .map((line, index) => `${excerpt.startLine + index}`.padStart(5, " ") + "  " + line)
                    .join("\n")}
                </code></pre>
              )}
            </article>
          ))}
          {!excerpts.length && (
            <div className="issues-empty">No evidence files were recorded for this finding.</div>
          )}
        </section>

        <section className="evidence-view-pane">
          <h2>Recommended approach</h2>
          {finding.recommended_fix ? (
            <LazyMarkdownContent
              content={finding.recommended_fix}
              className="evidence-recommended-fix"
            />
          ) : (
            <div className="issues-empty">No recommended-fix guidance was recorded for this finding.</div>
          )}
          <h3>Investigation approach</h3>
          <ol>{finding.suggested_approach.map((item) => <li key={item}>{item}</li>)}</ol>
        </section>
      </div>
    </main>
  );
}
