import { useCallback, useRef, useState } from "react";

const COMMON_TOOLS = [
  "Bash",
  "Read",
  "Write",
  "Edit",
  "Glob",
  "Grep",
  "Bash(git *)",
  "Bash(npm *)",
  "Bash(npx *)",
  "Bash(uv *)",
  "Bash(python *)",
  "Bash(make *)",
  "Bash(cargo *)",
  "Bash(go *)",
  "LSP",
  "NotebookEdit",
  "WebFetch",
  "WebSearch",
];

interface TagInputProps {
  values: string[];
  onChange: (values: string[]) => void;
}

export default function TagInput({ values, onChange }: TagInputProps) {
  const [input, setInput] = useState("");
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [highlightIdx, setHighlightIdx] = useState(-1);
  const inputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const filtered = COMMON_TOOLS.filter(
    (t) =>
      !values.includes(t) &&
      t.toLowerCase().includes(input.toLowerCase()),
  );

  const addTag = useCallback(
    (tag: string) => {
      const trimmed = tag.trim();
      if (trimmed && !values.includes(trimmed)) {
        onChange([...values, trimmed]);
      }
      setInput("");
      setShowSuggestions(false);
      setHighlightIdx(-1);
    },
    [values, onChange],
  );

  const removeTag = useCallback(
    (idx: number) => {
      onChange(values.filter((_, i) => i !== idx));
    },
    [values, onChange],
  );

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      if (highlightIdx >= 0 && highlightIdx < filtered.length) {
        const suggestion = filtered[highlightIdx];
        if (suggestion) addTag(suggestion);
      } else if (input.trim()) {
        addTag(input);
      }
    } else if (e.key === "Backspace" && !input && values.length > 0) {
      removeTag(values.length - 1);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlightIdx((prev) => Math.min(prev + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlightIdx((prev) => Math.max(prev - 1, 0));
    } else if (e.key === "Escape") {
      setShowSuggestions(false);
    }
  };

  return (
    <div style={{ position: "relative" }} ref={containerRef}>
      <div
        className="tag-input-container"
        onClick={() => inputRef.current?.focus()}
      >
        {values.map((v, i) => (
          <span key={i} className="tag">
            {v}
            <button
              type="button"
              className="tag-remove"
              onClick={(e) => {
                e.stopPropagation();
                removeTag(i);
              }}
            >
              x
            </button>
          </span>
        ))}
        <input
          ref={inputRef}
          className="tag-text-input"
          value={input}
          onChange={(e) => {
            setInput(e.target.value);
            setShowSuggestions(true);
            setHighlightIdx(-1);
          }}
          onFocus={() => setShowSuggestions(true)}
          onBlur={() => {
            // Delay to allow click on suggestion.
            setTimeout(() => setShowSuggestions(false), 150);
          }}
          onKeyDown={handleKeyDown}
          placeholder="Add tool pattern..."
        />
      </div>
      {showSuggestions && input && filtered.length > 0 && (
        <div className="tag-suggestions">
          {filtered.slice(0, 8).map((s, i) => (
            <button
              key={s}
              type="button"
              className={`tag-suggestion${i === highlightIdx ? " highlighted" : ""}`}
              onMouseDown={(e) => {
                e.preventDefault();
                addTag(s);
              }}
            >
              {s}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
