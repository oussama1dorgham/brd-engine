import { useEffect, useRef, useState } from "react";

export default function Composer({
  disabled,
  placeholder,
  onSend,
  focusKey,
}: {
  disabled: boolean;
  placeholder: string;
  onSend: (q: string) => void;
  focusKey: number;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const [val, setVal] = useState("");
  const canSend = !disabled && val.trim() !== "";

  const autosize = () => {
    const el = ref.current;
    if (el) { el.style.height = "auto"; el.style.height = Math.min(el.scrollHeight, 184) + "px"; }
  };
  const submit = () => {
    const q = val.trim();
    if (q && !disabled) { onSend(q); setVal(""); if (ref.current) ref.current.style.height = "auto"; }
  };

  useEffect(() => { if (!disabled) ref.current?.focus(); }, [disabled, focusKey]);

  return (
    <div className="composer">
      <div className="cbox">
        <textarea
          ref={ref}
          rows={1}
          placeholder={placeholder}
          disabled={disabled}
          value={val}
          onChange={(e) => { setVal(e.target.value); autosize(); }}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
        />
        <button className="send" aria-label="Send message" disabled={!canSend} onClick={submit}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.3} strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="19" x2="12" y2="5" />
            <polyline points="5 12 12 5 19 12" />
          </svg>
        </button>
      </div>
      <div className="hint">
        <span>Grounded in the document · cites each requirement</span>
        <span className="sep"></span>
        <span><kbd>Enter</kbd> send</span>
        <span className="sep"></span>
        <span><kbd>Shift</kbd>+<kbd>Enter</kbd> newline</span>
      </div>
    </div>
  );
}
