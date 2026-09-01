import { lazy, Suspense } from "react";

const MarkdownContent = lazy(() => import("./MarkdownContent"));

interface LazyMarkdownContentProps {
  content: string;
  className?: string;
}

export function LazyMarkdownContent(props: LazyMarkdownContentProps) {
  return (
    <Suspense fallback={<div className="markdown-content">{props.content}</div>}>
      <MarkdownContent {...props} />
    </Suspense>
  );
}
