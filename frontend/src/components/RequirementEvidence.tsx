import { reqLabel } from "../lib/cite";
import type { Requirement } from "../lib/api";

// Present one cited requirement readably. Heading-less BRD chunks are often flattened
// tables (pipe-delimited, no newlines); rather than dump the raw blob we break those on
// " | " into scannable cells. Normal prose is shown as paragraphs.
function render(text: string): { table: boolean; parts: string[] } {
  const t = (text || "").replace(/\r/g, "").trim();
  if (t.includes("\n")) return { table: false, parts: t.split(/\n+/).map((s) => s.trim()).filter(Boolean) };
  if ((t.match(/\|/g) || []).length >= 2) return { table: true, parts: t.split(/\s*\|\s*/).map((s) => s.trim()).filter(Boolean) };
  return { table: false, parts: [t] };
}

export default function RequirementEvidence({ req, title }: { req: Requirement; title?: string }) {
  const { table, parts } = render(req.text);
  return (
    <div className="uc-ev" dir="auto">
      <div className="uc-ev-title">{title || reqLabel(req)}</div>
      {table ? (
        <div className="uc-ev-cells">
          {parts.map((p, i) => <span key={i} className="uc-ev-cell">{p}</span>)}
        </div>
      ) : (
        <div className="uc-ev-text">{parts.map((p, i) => <p key={i}>{p}</p>)}</div>
      )}
    </div>
  );
}
