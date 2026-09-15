import { useEffect, useRef, useState } from "react";

// In-app replacement for window.prompt: a modal with a single text input.
// Resolves to the trimmed value, or null on cancel / escape / empty.
export interface PromptState {
  title: string;
  defaultValue: string;
  okLabel: string;
  placeholder?: string;
  resolve: (v: string | null) => void;
}

export default function PromptModal({ state }: { state: PromptState | null }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [val, setVal] = useState("");

  useEffect(() => {
    if (!state) return;
    setVal(state.defaultValue);
    const t = setTimeout(() => { inputRef.current?.focus(); inputRef.current?.select(); }, 40);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); state.resolve(null); }
    };
    document.addEventListener("keydown", onKey);
    return () => { clearTimeout(t); document.removeEventListener("keydown", onKey); };
  }, [state]);

  if (!state) return null;
  const submit = () => state.resolve(val.trim() || null);

  return (
    <div className="modal-scrim confirm-scrim" onMouseDown={(e) => { if (e.target === e.currentTarget) state.resolve(null); }}>
      <div className="modal confirm" role="dialog" aria-modal="true">
        <div className="confirm-body">
          <h2>{state.title}</h2>
          <input
            ref={inputRef}
            className="prompt-input"
            value={val}
            placeholder={state.placeholder || ""}
            dir="auto"
            onChange={(e) => setVal(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } }}
          />
        </div>
        <div className="confirm-actions">
          <button className="btn-ghost" onClick={() => state.resolve(null)}>Cancel</button>
          <button className="btn-primary" onClick={submit}>{state.okLabel}</button>
        </div>
      </div>
    </div>
  );
}
