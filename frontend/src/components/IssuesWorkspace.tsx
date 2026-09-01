import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Bot,
  CircleDot,
  ExternalLink,
  GitPullRequestClosed,
  History,
  LoaderCircle,
  MessageSquare,
} from "lucide-react";
import {
  fetchIssueInvestigation,
  fetchIssueTimeline,
  fetchIssues,
  investigateIssue,
  investigateIssues,
} from "../api";
import { EvidenceView } from "./EvidenceView";
import type {
  AnalysisReport,
  GitHubIssue,
  IssueTimeline,
  IssueWorkspaceReport,
  ProposedIssue,
} from "../types";

type IssueTab = "github" | "proposed";
type StateFilter = "all" | "open" | "closed";
type EvidenceTarget = { findingId?: string; issueNumber?: number };

function ProposalCard({
  finding,
  onInspect,
}: {
  finding: ProposedIssue;
  onInspect: () => void;
}) {
  return (
    <article>
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
      <h2>{finding.title}</h2>
      <p>{finding.explanation}</p>
      <h3>Investigation approach</h3>
      <ol>{finding.suggested_approach.map((item) => <li key={item}>{item}</li>)}</ol>
      {finding.evidence[0] && <code>{finding.evidence[0].path}:L{finding.evidence[0].start_line}</code>}
      {finding.evidence.length > 0 && (
        <button type="button" onClick={onInspect}>
          Inspect evidence <ArrowRight size={14} />
        </button>
      )}
    </article>
  );
}

export function IssuesWorkspace({
  report,
}: {
  report: AnalysisReport;
}) {
  const [workspace, setWorkspace] = useState<IssueWorkspaceReport | null>(null);
  const [tab, setTab] = useState<IssueTab>("github");
  const [stateFilter, setStateFilter] = useState<StateFilter>("all");
  const [selectedIssue, setSelectedIssue] = useState<GitHubIssue | null>(null);
  const [timeline, setTimeline] = useState<IssueTimeline | null>(null);
  const [timelineLoading, setTimelineLoading] = useState(false);
  const [issueInvestigation, setIssueInvestigation] = useState<ProposedIssue | null>(null);
  const [investigatingIssue, setInvestigatingIssue] = useState(false);
  const [investigating, setInvestigating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [evidenceTarget, setEvidenceTarget] = useState<EvidenceTarget | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    fetchIssues(report.analysis_id)
      .then((result) => {
        if (!cancelled) setWorkspace(result);
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : "Issues failed to load");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [report.analysis_id]);

  const visibleIssues = useMemo(
    () =>
      (workspace?.github_issues ?? []).filter(
        (issue) => stateFilter === "all" || issue.state === stateFilter,
      ),
    [workspace, stateFilter],
  );

  const hasAiFindings = (workspace?.proposed_issues ?? []).some(
    (item) => item.source === "ai_finding",
  );

  const selectIssue = async (issue: GitHubIssue) => {
    setSelectedIssue(issue);
    setTimeline(null);
    setIssueInvestigation(null);
    setTimelineLoading(true);
    try {
      setTimeline(await fetchIssueTimeline(report.analysis_id, issue.number));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Issue history failed");
    } finally {
      setTimelineLoading(false);
    }
    try {
      setIssueInvestigation(await fetchIssueInvestigation(report.analysis_id, issue.number));
    } catch {
      // A cached-investigation lookup failure just means nothing to show yet - the
      // user can still click "Investigate this issue"; don't surface a second error
      // banner for it.
    }
  };

  const investigate = async () => {
    setInvestigating(true);
    setError(null);
    try {
      await investigateIssues(report.analysis_id);
      setWorkspace(await fetchIssues(report.analysis_id));
      setTab("proposed");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Investigation failed");
    } finally {
      setInvestigating(false);
    }
  };

  const investigateThisIssue = async () => {
    if (!selectedIssue) return;
    setInvestigatingIssue(true);
    setError(null);
    try {
      setIssueInvestigation(await investigateIssue(report.analysis_id, selectedIssue.number));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Issue investigation failed");
    } finally {
      setInvestigatingIssue(false);
    }
  };

  if (evidenceTarget) {
    return (
      <EvidenceView
        analysisId={report.analysis_id}
        findingId={evidenceTarget.findingId}
        issueNumber={evidenceTarget.issueNumber}
        onBack={() => setEvidenceTarget(null)}
      />
    );
  }

  if (error && !workspace) return <div className="insights-state error">{error}</div>;
  if (!workspace) {
    return (
      <div className="insights-state">
        <LoaderCircle className="spin" size={24} />
        Synchronizing repository issues…
      </div>
    );
  }

  const openCount = workspace.github_issues.filter((item) => item.state === "open").length;
  const closedCount = workspace.github_issues.filter((item) => item.state === "closed").length;

  return (
    <main className="issues-workspace">
      <header className="issues-header">
        <div>
          <div className="onboarding-kicker">Repository work and risks</div>
          <h1>Issues, history, and evidence-backed findings.</h1>
          <p>
            GitHub issues remain separate from Waypoint proposals. Generated findings
            are never presented as maintainer-confirmed defects.
          </p>
        </div>
        <div className="issue-metrics">
          <div><CircleDot size={17} /><strong>{openCount}</strong><span>open loaded</span></div>
          <div><GitPullRequestClosed size={17} /><strong>{closedCount}</strong><span>closed loaded</span></div>
          <div><Bot size={17} /><strong>{workspace.proposed_issues.length}</strong><span>proposals</span></div>
        </div>
      </header>

      {workspace.synchronization_warning && (
        <div className="issue-warning"><AlertTriangle size={15} />{workspace.synchronization_warning}</div>
      )}

      <div className="issues-tabs">
        <button className={tab === "github" ? "active" : ""} onClick={() => setTab("github")}>
          GitHub history
        </button>
        <button className={tab === "proposed" ? "active" : ""} onClick={() => setTab("proposed")}>
          Waypoint proposals
        </button>
        {tab === "github" && (
          <div className="issue-state-filter">
            {(["all", "open", "closed"] as StateFilter[]).map((state) => (
              <button key={state} className={stateFilter === state ? "active" : ""} onClick={() => setStateFilter(state)}>
                {state}
              </button>
            ))}
          </div>
        )}
        {tab === "proposed" && (
          <button
            type="button"
            className="investigate-issues"
            disabled={investigating}
            onClick={() => void investigate()}
          >
            {investigating ? <LoaderCircle className="spin" size={13} /> : <Bot size={13} />}
            {investigating ? "Investigating…" : hasAiFindings ? "Generate more" : "Investigate with model"}
          </button>
        )}
      </div>
      {error && <div className="issue-warning"><AlertTriangle size={15} />{error}</div>}

      {tab === "github" ? (
        <div className="issue-browser">
          <section className="issue-list">
            {visibleIssues.map((issue) => (
              <button
                type="button"
                key={issue.number}
                className={selectedIssue?.number === issue.number ? "active" : ""}
                onClick={() => void selectIssue(issue)}
              >
                <span className={`issue-state ${issue.state}`}>{issue.state}</span>
                <strong>#{issue.number} {issue.title}</strong>
                <small>{issue.author ?? "unknown"} · {issue.comments} comments · updated {new Date(issue.updated_at).toLocaleDateString()}</small>
                <span className="issue-labels">{issue.labels.map((label) => <em key={label}>{label}</em>)}</span>
              </button>
            ))}
            {!visibleIssues.length && (
              <div className="issues-empty">No {stateFilter === "all" ? "" : stateFilter} GitHub issues were loaded.</div>
            )}
          </section>
          <aside className="issue-detail">
            {selectedIssue ? (
              <>
                <div className="issue-detail-heading">
                  <div><span>#{selectedIssue.number}</span><h2>{selectedIssue.title}</h2></div>
                  <div className="issue-detail-actions">
                    <a href={selectedIssue.url} target="_blank" rel="noreferrer">GitHub <ExternalLink size={13} /></a>
                    <button
                      type="button"
                      className="investigate-issues"
                      disabled={investigatingIssue}
                      onClick={() => void investigateThisIssue()}
                    >
                      {investigatingIssue ? <LoaderCircle className="spin" size={13} /> : <Bot size={13} />}
                      {investigatingIssue
                        ? "Investigating…"
                        : issueInvestigation
                          ? "Re-investigate this issue"
                          : "Investigate this issue"}
                    </button>
                  </div>
                </div>
                <p>{selectedIssue.body || "This issue has no description."}</p>
                <h3><History size={15} /> Issue history</h3>
                {timelineLoading ? <LoaderCircle className="spin" size={18} /> : (
                  <ol className="issue-timeline">
                    {(timeline?.events ?? []).map((event) => (
                      <li key={event.id}>
                        <CircleDot size={11} />
                        <div><strong>{event.description}</strong><small>{event.actor ?? "GitHub"} · {event.created_at ? new Date(event.created_at).toLocaleString() : "time unavailable"}</small></div>
                      </li>
                    ))}
                  </ol>
                )}
                {issueInvestigation && (
                  <>
                    <h3><Bot size={15} /> Waypoint investigation</h3>
                    <section className="proposal-list">
                      <ProposalCard
                        finding={issueInvestigation}
                        onInspect={() =>
                          setEvidenceTarget(
                            issueInvestigation.github_issue_number
                              ? { issueNumber: issueInvestigation.github_issue_number }
                              : { findingId: issueInvestigation.id },
                          )
                        }
                      />
                    </section>
                  </>
                )}
              </>
            ) : (
              <div className="issues-empty"><MessageSquare size={25} />Select an issue to inspect its description, timeline, and investigate it with the model.</div>
            )}
          </aside>
        </div>
      ) : (
        <section className="proposal-list">
          {workspace.proposed_issues.map((finding) => (
            <ProposalCard
              key={finding.id}
              finding={finding}
              onInspect={() =>
                setEvidenceTarget(
                  finding.github_issue_number
                    ? { issueNumber: finding.github_issue_number }
                    : { findingId: finding.id },
                )
              }
            />
          ))}
          {!workspace.proposed_issues.length && <div className="issues-empty">No evidence-backed issue candidates were detected.</div>}
        </section>
      )}
    </main>
  );
}
