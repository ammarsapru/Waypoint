import { useEffect, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Boxes,
  CircleDot,
  GitCompareArrows,
  LoaderCircle,
} from "lucide-react";
import { compareAnalyses, fetchArchitecture } from "../api";
import type {
  AnalysisReport,
  ArchitectureReport,
  RevisionReport,
} from "../types";

export function InsightsWorkspace({
  report,
  baseAnalysisId,
  onOpenNode,
}: {
  report: AnalysisReport;
  baseAnalysisId: string | null;
  onOpenNode: (nodeId: string) => void;
}) {
  const [architecture, setArchitecture] = useState<ArchitectureReport | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState<RevisionReport | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchArchitecture(report.analysis_id)
      .then((result) => {
        if (!cancelled) setArchitecture(result);
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setError(
            caught instanceof Error
              ? caught.message
              : "Architecture inspection failed",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [report.analysis_id]);

  useEffect(() => {
    if (!baseAnalysisId) {
      setRevision(null);
      return;
    }
    let cancelled = false;
    compareAnalyses(report.analysis_id, baseAnalysisId)
      .then((result) => {
        if (!cancelled) setRevision(result);
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setError(
            caught instanceof Error ? caught.message : "Comparison failed",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [report.analysis_id, baseAnalysisId]);

  if (error) {
    return <div className="insights-state error">{error}</div>;
  }
  if (!architecture) {
    return (
      <div className="insights-state">
        <LoaderCircle className="spin" size={24} />
        Inspecting module architecture…
      </div>
    );
  }
  return (
    <main className="insights-workspace">
      <header className="insights-header">
        <div>
          <div className="onboarding-kicker">Architecture evidence</div>
          <h1>What the dependency graph says about this system.</h1>
          <p>
            These findings are derived from verified module imports and graph
            connectivity. They are signals for investigation, not automatic
            declarations of defects.
          </p>
        </div>
        <div className="insight-metrics">
          <div>
            <GitCompareArrows size={18} />
            <strong>{architecture.import_cycle_count}</strong>
            <span>import cycles</span>
          </div>
          <div>
            <CircleDot size={18} />
            <strong>{architecture.hotspot_count}</strong>
            <span>hotspots</span>
          </div>
          <div>
            <Boxes size={18} />
            <strong>{report.stats.files_parsed}</strong>
            <span>modules inspected</span>
          </div>
        </div>
      </header>
      <section className="revision-panel">
        <div>
          <GitCompareArrows size={18} />
          <div>
            <strong>Change-aware refresher</strong>
            <span>
              {revision
                ? `${revision.modified.length} modified · ${revision.added.length} added · ${revision.removed.length} removed`
                : "Analyze this repository again to compare revisions"}
            </span>
          </div>
        </div>
        {revision && (
          <div className="revision-content">
            {revision.refresher.length ? (
              revision.refresher.map((item) => <code key={item}>{item}</code>)
            ) : (
              <p>No module source changed; prior knowledge remains current.</p>
            )}
          </div>
        )}
      </section>
      <section className="insights-list">
        {architecture.insights.length ? (
          architecture.insights.map((insight) => (
            <article key={insight.id} className="insight-card">
              <div className={`insight-icon ${insight.severity}`}>
                <AlertTriangle size={18} />
              </div>
              <div>
                <div className="insight-category">{insight.category}</div>
                <h2>{insight.title}</h2>
                <p>{insight.explanation}</p>
                {insight.evidence[0] && (
                  <span className="insight-evidence">
                    {insight.evidence[0].path}:L
                    {insight.evidence[0].start_line}
                  </span>
                )}
              </div>
              {insight.node_ids[0] && (
                <button
                  type="button"
                  onClick={() => onOpenNode(insight.node_ids[0])}
                >
                  Inspect <ArrowRight size={15} />
                </button>
              )}
            </article>
          ))
        ) : (
          <div className="no-insights">
            <Boxes size={30} />
            <h2>No cycles or high-connectivity hotspots crossed the thresholds.</h2>
            <p>The graph still remains available for manual inspection.</p>
          </div>
        )}
      </section>
    </main>
  );
}
