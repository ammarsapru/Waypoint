import { useEffect, useState } from "react";
import {
  ArrowDownLeft,
  ArrowUpRight,
  Braces,
  CheckCircle2,
  GitBranch,
  LoaderCircle,
  MapPin,
  MessageSquareText,
  Send,
  ShieldQuestion,
} from "lucide-react";
import { askRepository, fetchSymbolUsage } from "../api";
import type {
  GraphNode,
  GroundedAnswer,
  Selection,
  SymbolUsage,
  SymbolUsageRelationship,
} from "../types";
import { LazyMarkdownContent } from "./LazyMarkdownContent";

type InspectorTab = "details" | "usage" | "ask";

function RelationshipList({
  title,
  items,
  onOpenNode,
}: {
  title: string;
  items: SymbolUsageRelationship[];
  onOpenNode: (nodeId: string) => void;
}) {
  return (
    <section className="usage-group">
      <div className="detail-label">{title}</div>
      {items.length ? (
        items.map((item) => (
          <button
            type="button"
            className="usage-relationship"
            key={`${item.direction}:${item.edge_id}`}
            onClick={() => onOpenNode(item.symbol.node_id)}
          >
            {item.direction === "incoming" ? (
              <ArrowDownLeft size={14} />
            ) : (
              <ArrowUpRight size={14} />
            )}
            <span>
              <strong>{item.symbol.qualified_name}</strong>
              <small>
                {item.relationship.replace("_", " ")} · {item.symbol.path ?? "unknown file"}
                {item.evidence.span.start_line
                  ? `:${item.evidence.span.start_line}`
                  : ""}
              </small>
              <em>{item.resolution}</em>
            </span>
          </button>
        ))
      ) : (
        <p className="usage-empty">No resolved relationships in this direction.</p>
      )}
    </section>
  );
}

export function Inspector({
  selection,
  nodesById,
  analysisId,
  onOpenNode,
}: {
  selection: Selection;
  nodesById: Map<string, GraphNode>;
  analysisId: string;
  onOpenNode: (nodeId: string) => void;
}) {
  const [tab, setTab] = useState<InspectorTab>("details");
  const [usage, setUsage] = useState<SymbolUsage | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageError, setUsageError] = useState<string | null>(null);
  const [question, setQuestion] = useState("How is this symbol used by other files, and for what purpose?");
  const [answer, setAnswer] = useState<GroundedAnswer | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [askError, setAskError] = useState<string | null>(null);
  const selectedNode = selection?.type === "node" ? selection.value : null;

  useEffect(() => {
    setTab("details");
    setUsage(null);
    setUsageError(null);
    setAnswer(null);
    setConversationId(null);
    setAskError(null);
  }, [selectedNode?.id]);

  useEffect(() => {
    if (!selectedNode || tab === "details" || usage) return;
    let active = true;
    setUsageLoading(true);
    fetchSymbolUsage(analysisId, selectedNode.id)
      .then((result) => {
        if (active) setUsage(result);
      })
      .catch((caught: unknown) => {
        if (active) {
          setUsageError(caught instanceof Error ? caught.message : "Symbol usage failed");
        }
      })
      .finally(() => {
        if (active) setUsageLoading(false);
      });
    return () => {
      active = false;
    };
  }, [analysisId, selectedNode, tab, usage]);

  const ask = async () => {
    if (!selectedNode || !question.trim() || asking) return;
    setAsking(true);
    setAskError(null);
    try {
      const result = await askRepository(
        analysisId,
        question.trim(),
        [],
        conversationId,
        "inspector",
        selectedNode.id,
      );
      setAnswer(result);
      setConversationId(result.conversation_id ?? conversationId);
    } catch (caught) {
      setAskError(caught instanceof Error ? caught.message : "Symbol question failed");
    } finally {
      setAsking(false);
    }
  };

  if (!selection) {
    return (
      <div className="empty-inspector">
        <ShieldQuestion size={28} />
        <h3>Inspect the evidence</h3>
        <p>Select a class, function, method, or relationship to inspect its usage.</p>
      </div>
    );
  }

  if (selection.type === "node") {
    const node = selection.value;
    return (
      <div className="inspector-content symbol-inspector">
        <div className="eyebrow">Selected {node.kind}</div>
        <h2>{node.name}</h2>
        <p className="qualified-name">{node.qualified_name}</p>

        <div className="inspector-tabs" role="tablist" aria-label="Symbol inspector">
          {(["details", "usage", "ask"] as InspectorTab[]).map((item) => (
            <button
              type="button"
              role="tab"
              aria-selected={tab === item}
              className={tab === item ? "active" : ""}
              key={item}
              onClick={() => setTab(item)}
            >
              {item === "ask" ? "Ask AI" : item[0].toUpperCase() + item.slice(1)}
            </button>
          ))}
        </div>

        {tab === "details" && (
          <>
            {node.signature && (
              <section className="detail-block">
                <div className="detail-label"><Braces size={14} /> Signature</div>
                <code>{node.signature}</code>
              </section>
            )}
            {node.span && (
              <section className="detail-block">
                <div className="detail-label"><MapPin size={14} /> Source</div>
                <strong>{node.span.path}</strong>
                <span>Lines {node.span.start_line}–{node.span.end_line}</span>
              </section>
            )}
            <section className="detail-block">
              <div className="detail-label">Stable identity</div>
              <code>{node.id}</code>
            </section>
          </>
        )}

        {tab === "usage" && (
          <div className="symbol-usage-panel">
            {usageLoading && <p><LoaderCircle className="spin" size={15} /> Loading relationships…</p>}
            {usageError && <p className="inline-error">{usageError}</p>}
            {usage && (
              <>
                <div className="usage-summary">
                  <strong>{usage.related_files.length}</strong>
                  <span>related files</span>
                  <strong>{usage.incoming.length}</strong>
                  <span>incoming</span>
                  <strong>{usage.outgoing.length}</strong>
                  <span>outgoing</span>
                </div>
                <RelationshipList title="Used by / incoming" items={usage.incoming} onOpenNode={onOpenNode} />
                <RelationshipList title="Uses / outgoing" items={usage.outgoing} onOpenNode={onOpenNode} />
              </>
            )}
          </div>
        )}

        {tab === "ask" && (
          <div className="symbol-chat">
            <div className="detail-label"><MessageSquareText size={14} /> Ask about {node.name}</div>
            <textarea
              value={question}
              rows={4}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="Ask how this symbol is used across the repository…"
            />
            <button type="button" className="symbol-chat-send" onClick={ask} disabled={asking || !question.trim()}>
              {asking ? <LoaderCircle className="spin" size={14} /> : <Send size={14} />}
              {asking ? "Investigating…" : "Ask with usage evidence"}
            </button>
            {askError && <p className="inline-error">{askError}</p>}
            {answer && (
              <div className="symbol-chat-answer">
                <LazyMarkdownContent content={answer.answer} />
                {!!answer.citations.length && (
                  <div className="symbol-chat-citations">
                    <strong>Evidence files</strong>
                    {answer.citations.map((citation, index) => (
                      <button
                        type="button"
                        key={`${citation.span.path}:${citation.span.start_line}:${index}`}
                        onClick={() => citation.node_id && onOpenNode(citation.node_id)}
                      >
                        {citation.span.path}:L{citation.span.start_line}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  const edge = selection.value;
  const source = nodesById.get(edge.source);
  const target = nodesById.get(edge.target);
  const confidence = Math.round(edge.evidence.confidence * 100);
  return (
    <div className="inspector-content">
      <div className="eyebrow">Selected relationship</div>
      <h2>{edge.kind.replace("_", " ")}</h2>
      <div className="relationship-route">
        <strong>{source?.name ?? edge.source}</strong>
        <GitBranch size={16} />
        <strong>{target?.name ?? edge.target}</strong>
      </div>
      <section className="evidence-status">
        <CheckCircle2 size={17} />
        <div><strong>{edge.evidence.status}</strong><span>{confidence}% confidence</span></div>
      </section>
      <section className="detail-block">
        <div className="detail-label">Why this edge exists</div>
        <p>{edge.evidence.resolution}</p>
      </section>
      <section className="detail-block">
        <div className="detail-label">Observed syntax</div>
        <code>{edge.evidence.syntax}</code>
      </section>
      <section className="detail-block">
        <div className="detail-label"><MapPin size={14} /> Evidence</div>
        <strong>{edge.evidence.span.path}</strong>
        <span>Line {edge.evidence.span.start_line}, column {edge.evidence.span.start_column}</span>
      </section>
    </div>
  );
}
