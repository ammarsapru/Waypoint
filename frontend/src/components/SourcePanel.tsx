import { useCallback, useEffect, useRef } from "react";
import Editor, { type OnMount } from "@monaco-editor/react";
import type { editor } from "monaco-editor";
import { Code2, FileWarning, LoaderCircle } from "lucide-react";
import type { SourceDocument, SourceSpan } from "../types";

export function SourcePanel({
  source,
  span,
  loading,
  error,
  theme,
}: {
  source: SourceDocument | null;
  span: SourceSpan | null;
  loading: boolean;
  error: string | null;
  theme: "light" | "dark";
}) {
  const editorRef = useRef<editor.IStandaloneCodeEditor | null>(null);
  const decorationsRef = useRef<editor.IEditorDecorationsCollection | null>(
    null,
  );

  const focusSpan = useCallback(() => {
    const activeEditor = editorRef.current;
    if (!activeEditor || !span) return;
    const model = activeEditor.getModel();
    if (!model) return;
    const startLine = Math.min(span.start_line, model.getLineCount());
    const endLine = Math.min(
      Math.max(startLine, span.end_line),
      model.getLineCount(),
    );
    activeEditor.setSelection({
      startLineNumber: startLine,
      startColumn: 1,
      endLineNumber: endLine,
      endColumn: Math.max(1, model.getLineMaxColumn(endLine)),
    });
    activeEditor.revealLineInCenter(startLine, 0);
    decorationsRef.current?.clear();
    decorationsRef.current = activeEditor.createDecorationsCollection([
      {
        range: {
          startLineNumber: startLine,
          startColumn: 1,
          endLineNumber: endLine,
          endColumn: Math.max(1, model.getLineMaxColumn(endLine)),
        },
        options: {
          isWholeLine: true,
          className: "focused-source-line",
          linesDecorationsClassName: "focused-source-gutter",
        },
      },
    ]);
  }, [span]);

  const handleMount: OnMount = (mountedEditor) => {
    editorRef.current = mountedEditor;
    requestAnimationFrame(focusSpan);
  };

  useEffect(() => {
    const frame = requestAnimationFrame(focusSpan);
    const retry = window.setTimeout(focusSpan, 80);
    return () => {
      cancelAnimationFrame(frame);
      window.clearTimeout(retry);
    };
  }, [focusSpan, source]);

  if (loading) {
    return (
      <div className="source-state">
        <LoaderCircle className="spin" size={24} />
        <span>Loading indexed source…</span>
      </div>
    );
  }
  if (error) {
    return (
      <div className="source-state error">
        <FileWarning size={24} />
        <span>{error}</span>
      </div>
    );
  }
  if (!source) {
    return (
      <div className="source-state">
        <Code2 size={24} />
        <span>Select a source-backed node or edge.</span>
      </div>
    );
  }
  return (
    <Editor
      height="100%"
      language={source.language}
      value={source.content}
      path={source.path}
      onMount={handleMount}
      theme={theme === "dark" ? "vs-dark" : "vs"}
      options={{
        readOnly: true,
        minimap: { enabled: true, scale: 0.75 },
        fontFamily:
          "'JetBrains Mono', 'Cascadia Code', Consolas, monospace",
        fontSize: 13,
        lineHeight: 21,
        renderWhitespace: "selection",
        scrollBeyondLastLine: false,
        smoothScrolling: true,
        padding: { top: 14, bottom: 14 },
        overviewRulerBorder: false,
        automaticLayout: true,
      }}
    />
  );
}
