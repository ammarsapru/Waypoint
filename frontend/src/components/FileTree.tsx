import { useMemo, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  FileCode2,
  Folder,
  FolderOpen,
  Link2,
} from "lucide-react";
import type { GraphNode, RepositoryAlias } from "../types";

interface TreeItem {
  name: string;
  path: string;
  children: TreeItem[];
  fileNode?: GraphNode;
  alias?: RepositoryAlias;
}

function buildTree(
  moduleNodes: GraphNode[],
  paths: string[],
  aliases: RepositoryAlias[],
): TreeItem[] {
  const root: TreeItem = { name: "", path: "", children: [] };
  const nodesByPath = new Map(
    moduleNodes.flatMap((node) => node.span ? [[node.span.path, node] as const] : []),
  );
  const allPaths = new Set([
    ...paths,
    ...nodesByPath.keys(),
  ]);
  const aliasesByPath = new Map(aliases.map((alias) => [alias.path, alias]));
  for (const sourcePath of allPaths) {
    const parts = sourcePath.split("/");
    let current = root;
    parts.forEach((part, index) => {
      const path = parts.slice(0, index + 1).join("/");
      let child = current.children.find((item) => item.name === part);
      if (!child) {
        child = { name: part, path, children: [] };
        current.children.push(child);
      }
      if (index === parts.length - 1) {
        child.fileNode = nodesByPath.get(sourcePath);
        child.alias = aliasesByPath.get(sourcePath);
      }
      current = child;
    });
  }
  const sort = (items: TreeItem[]) => {
    items.sort((a, b) => {
      const aFolder = a.children.length > 0;
      const bFolder = b.children.length > 0;
      if (aFolder !== bFolder) return aFolder ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    items.forEach((item) => sort(item.children));
  };
  sort(root.children);
  return root.children;
}

function TreeRow({
  item,
  depth,
  selectedPath,
  onSelect,
}: {
  item: TreeItem;
  depth: number;
  selectedPath?: string;
  onSelect: (path: string, node?: GraphNode) => void;
}) {
  const [open, setOpen] = useState(depth < 2);
  const isFolder = item.children.length > 0 && !item.fileNode;
  return (
    <>
      <button
        type="button"
        className={`tree-row ${selectedPath === item.path ? "selected" : ""}`}
        style={{ paddingLeft: 12 + depth * 15 }}
        onClick={() => {
          if (isFolder) setOpen((value) => !value);
          else if (item.alias?.target_kind !== "directory") {
            onSelect(item.path, item.fileNode);
          }
        }}
      >
        {isFolder ? (
          <>
            {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            {open ? <FolderOpen size={15} /> : <Folder size={15} />}
          </>
        ) : (
          <>
            <span className="tree-spacer" />
            {item.alias ? <Link2 size={15} /> : <FileCode2 size={15} />}
          </>
        )}
        <span title={item.path}>{item.name}</span>
        {item.alias && (
          <small
            className="tree-alias-badge"
            title={`Alias to ${item.alias.canonical_path} (${item.alias.reason.replaceAll("_", " ")})`}
          >
            alias
          </small>
        )}
      </button>
      {isFolder &&
        open &&
        item.children.map((child) => (
          <TreeRow
            key={child.path}
            item={child}
            depth={depth + 1}
            selectedPath={selectedPath}
            onSelect={onSelect}
          />
        ))}
    </>
  );
}

export function FileTree({
  nodes,
  paths,
  aliases,
  selectedPath,
  onSelect,
}: {
  nodes: GraphNode[];
  selectedPath?: string;
  paths: string[];
  aliases: RepositoryAlias[];
  onSelect: (path: string, node?: GraphNode) => void;
}) {
  const tree = useMemo(
    () => buildTree(nodes.filter((node) => node.kind === "module"), paths, aliases),
    [nodes, paths, aliases],
  );
  return (
    <div className="file-tree">
      {tree.map((item) => (
        <TreeRow
          key={item.path}
          item={item}
          depth={0}
          selectedPath={selectedPath}
          onSelect={onSelect}
        />
      ))}
    </div>
  );
}
