import { useState, useCallback, useMemo, useRef, useEffect } from "react";
import type { Hunk } from "../lib/types";

interface FilePickerProps {
  hunks: Hunk[];
  currentIndex: number;
  onJumpToFile: (hunkIndex: number) => void;
  disabled?: boolean;
}

interface FileEntry {
  path: string;
  firstHunkIndex: number;
  hunkCount: number;
  stagedCount: number;
}

export function FilePicker({
  hunks,
  currentIndex,
  onJumpToFile,
  disabled,
}: FilePickerProps) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);

  const currentFile = hunks[currentIndex]?.file_path ?? null;

  const files: FileEntry[] = useMemo(() => {
    const map = new Map<string, FileEntry>();
    for (let i = 0; i < hunks.length; i++) {
      const h = hunks[i]!;
      const existing = map.get(h.file_path);
      if (existing) {
        existing.hunkCount++;
        if (h.staged) existing.stagedCount++;
      } else {
        map.set(h.file_path, {
          path: h.file_path,
          firstHunkIndex: i,
          hunkCount: 1,
          stagedCount: h.staged ? 1 : 0,
        });
      }
    }
    return Array.from(map.values());
  }, [hunks]);

  const handleSelect = useCallback(
    (hunkIndex: number) => {
      onJumpToFile(hunkIndex);
      setOpen(false);
    },
    [onJumpToFile],
  );

  // Close on outside tap
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent | TouchEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    document.addEventListener("touchstart", handler);
    return () => {
      document.removeEventListener("mousedown", handler);
      document.removeEventListener("touchstart", handler);
    };
  }, [open]);

  // Basename for compact display
  const basename = currentFile
    ? currentFile.split("/").pop() ?? currentFile
    : "—";

  return (
    <div className="file-picker" ref={containerRef}>
      <button
        className="file-picker-btn"
        onClick={() => setOpen((o) => !o)}
        disabled={disabled}
        title={currentFile ?? "Select file"}
      >
        <span className="file-picker-name">{basename}</span>
        <span className="file-picker-chevron">{open ? "▲" : "▼"}</span>
      </button>

      {open && (
        <div className="file-picker-dropdown">
          {files.map((f) => {
            const isActive = f.path === currentFile;
            const allStaged = f.stagedCount === f.hunkCount;
            return (
              <button
                key={f.path}
                className={`file-picker-item${isActive ? " active" : ""}`}
                onClick={() => handleSelect(f.firstHunkIndex)}
              >
                <span className="file-picker-item-path">{f.path}</span>
                <span className="file-picker-item-meta">
                  {allStaged ? (
                    <span className="file-picker-item-badge staged">
                      {f.hunkCount} staged
                    </span>
                  ) : f.stagedCount > 0 ? (
                    <span className="file-picker-item-badge partial">
                      {f.stagedCount}/{f.hunkCount}
                    </span>
                  ) : (
                    <span className="file-picker-item-badge">
                      {f.hunkCount} {f.hunkCount === 1 ? "hunk" : "hunks"}
                    </span>
                  )}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
