import { useCallback, useEffect, useRef, useState } from "react";
import { validatePath } from "../lib/api";
import type { AppConfig, ContextConfig } from "../lib/types";
import TagInput from "./TagInput";
import SandboxForm from "./SandboxForm";

interface ContextEditorProps {
  config: AppConfig;
  contextName: string | null; // null = new context
  onSave: (name: string, ctx: ContextConfig, isDefault: boolean) => void;
  onDelete: (name: string) => void;
  onBack: () => void;
}

type PathStatus = "idle" | "checking" | "valid" | "invalid";

const MODELS = [
  { value: "openai/gpt-5.5", label: "openai/gpt-5.5" },
  { value: "anthropic/claude-sonnet-4-6", label: "anthropic/claude-sonnet-4-6" },
] as const;

function usePathValidation() {
  const [status, setStatus] = useState<PathStatus>("idle");
  const timerRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  const check = useCallback((path: string) => {
    if (timerRef.current) clearTimeout(timerRef.current);
    if (!path.trim()) {
      setStatus("idle");
      return;
    }
    setStatus("checking");
    timerRef.current = setTimeout(async () => {
      try {
        const result = await validatePath(path);
        setStatus(result.exists ? "valid" : "invalid");
      } catch {
        setStatus("idle");
      }
    }, 500);
  }, []);

  return { status, check };
}

export default function ContextEditor({
  config,
  contextName,
  onSave,
  onDelete,
  onBack,
}: ContextEditorProps) {
  const existing = contextName ? config.contexts[contextName] : undefined;

  const [name, setName] = useState(contextName ?? "");
  const [directory, setDirectory] = useState(existing?.directory ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [model, setModel] = useState(existing?.model ?? "");
  const [allowedTools, setAllowedTools] = useState<string[]>(
    existing?.allowed_tools ?? [],
  );
  const [additionalDirs, setAdditionalDirs] = useState<string[]>(
    existing?.additional_directories ?? [],
  );
  const [defaultForChats, setDefaultForChats] = useState<string>(
    (existing?.default_for_chats ?? []).join(", "),
  );
  const [lockedForChats, setLockedForChats] = useState<string>(
    (existing?.locked_for_chats ?? []).join(", "),
  );
  const [sandbox, setSandbox] = useState(existing?.sandbox ?? null);
  const [isDefault, setIsDefault] = useState(
    contextName === config.default_context,
  );

  const dirValidation = usePathValidation();

  useEffect(() => {
    dirValidation.check(directory);
  }, [directory, dirValidation.check]);

  const handleSave = useCallback(() => {
    if (!name.trim() || !directory.trim() || !description.trim()) return;

    const parseChatIds = (s: string): number[] =>
      s
        .split(",")
        .map((x) => parseInt(x.trim()))
        .filter((x) => !isNaN(x));

    const ctx: ContextConfig = {
      directory,
      description,
      allowed_tools: allowedTools,
      model: model.trim() || null,
      additional_directories: additionalDirs.filter((d) => d.trim()),
      default_for_chats: parseChatIds(defaultForChats),
      locked_for_chats: parseChatIds(lockedForChats),
      sandbox,
    };
    onSave(name.trim(), ctx, isDefault);
  }, [
    name,
    directory,
    description,
    model,
    allowedTools,
    additionalDirs,
    defaultForChats,
    lockedForChats,
    sandbox,
    isDefault,
    onSave,
  ]);

  const canSave =
    name.trim() !== "" &&
    directory.trim() !== "" &&
    description.trim() !== "";

  return (
    <>
      <div className="app-header">
        <button type="button" className="app-header-back" onClick={onBack}>
          &lsaquo; Back
        </button>
        <h1>{contextName ? "Edit Context" : "New Context"}</h1>
        <div style={{ width: 48 }} />
      </div>

      <div className="form-section">
        <div className="form-group">
          <label className="form-label">Name</label>
          <input
            className="form-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my-project"
            disabled={contextName !== null}
          />
        </div>

        <div className="form-group">
          <label className="form-label">Directory</label>
          <input
            className={`form-input${dirValidation.status === "valid" ? " valid" : dirValidation.status === "invalid" ? " error" : ""}`}
            value={directory}
            onChange={(e) => setDirectory(e.target.value)}
            placeholder="/home/user/project"
          />
          {dirValidation.status === "checking" && (
            <span className="form-hint">Checking path...</span>
          )}
          {dirValidation.status === "valid" && (
            <span className="form-hint valid">Directory exists</span>
          )}
          {dirValidation.status === "invalid" && (
            <span className="form-hint error">Directory not found</span>
          )}
        </div>

        <div className="form-group">
          <label className="form-label">Description</label>
          <input
            className="form-input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What this context is for"
          />
        </div>

        <div className="form-group">
          <label className="form-label">Model</label>
          <input
            className="form-input"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            list="model-options"
            placeholder="provider/model, e.g. openai/gpt-5.5"
          />
          <datalist id="model-options">
            {MODELS.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </datalist>
        </div>

        <div className="form-group">
          <label className="form-label">Allowed Tools</label>
          <TagInput values={allowedTools} onChange={setAllowedTools} />
        </div>

        <div className="form-group">
          <label className="form-label">Additional Directories</label>
          <div className="list-input-items">
            {additionalDirs.map((d, i) => (
              <div key={i} className="list-input-row">
                <input
                  className="form-input"
                  value={d}
                  onChange={(e) => {
                    const next = [...additionalDirs];
                    next[i] = e.target.value;
                    setAdditionalDirs(next);
                  }}
                  placeholder="/path/to/directory"
                />
                <button
                  type="button"
                  className="list-input-remove"
                  onClick={() =>
                    setAdditionalDirs(additionalDirs.filter((_, j) => j !== i))
                  }
                >
                  x
                </button>
              </div>
            ))}
          </div>
          <button
            type="button"
            className="add-btn"
            onClick={() => setAdditionalDirs([...additionalDirs, ""])}
          >
            + Add Directory
          </button>
        </div>

        <div className="form-group">
          <label className="form-label">Default for Chats</label>
          <input
            className="form-input"
            value={defaultForChats}
            onChange={(e) => setDefaultForChats(e.target.value)}
            placeholder="Comma-separated chat IDs"
          />
          <span className="form-hint">
            Chat IDs that default to this context
          </span>
        </div>

        <div className="form-group">
          <label className="form-label">Locked for Chats</label>
          <input
            className="form-input"
            value={lockedForChats}
            onChange={(e) => setLockedForChats(e.target.value)}
            placeholder="Comma-separated chat IDs"
          />
          <span className="form-hint">
            Chat IDs locked to this context (cannot switch)
          </span>
        </div>

        <div className="form-toggle-row">
          <span className="form-toggle-label">Default Context</span>
          <button
            type="button"
            className={`toggle${isDefault ? " on" : ""}`}
            onClick={() => setIsDefault(!isDefault)}
          />
        </div>

        <SandboxForm sandbox={sandbox} onChange={setSandbox} />
      </div>

      {contextName && (
        <div className="delete-section">
          <button
            type="button"
            className="btn btn-danger"
            style={{ width: "100%" }}
            onClick={() => {
              if (
                Object.keys(config.contexts).length <= 1
              ) {
                alert("Cannot delete the last context");
                return;
              }
              if (confirm(`Delete context "${contextName}"?`)) {
                onDelete(contextName);
              }
            }}
          >
            Delete Context
          </button>
        </div>
      )}

      <div className="save-footer">
        <button
          type="button"
          className="btn btn-secondary"
          onClick={onBack}
        >
          Cancel
        </button>
        <button
          type="button"
          className="btn btn-success"
          onClick={handleSave}
          disabled={!canSave}
        >
          {contextName ? "Save" : "Create"}
        </button>
      </div>
    </>
  );
}
