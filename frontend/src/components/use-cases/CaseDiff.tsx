// Field-by-field comparison of one use case (old in red, new in green), used by the version
// review / compare dialog. Lists (steps, roles, citations) mark removed / added items individually.
export type CaseField = "title" | "description" | "roles" | "preconditions" | "steps"
  | "expected_behaviour" | "source_chunk_ids" | "case_type";

export const FIELD_LABEL: Record<CaseField, string> = {
  title: "Title", description: "Description", roles: "Roles", preconditions: "Preconditions", steps: "Steps",
  expected_behaviour: "Expected behaviour", source_chunk_ids: "Source requirements", case_type: "Case type",
};
export const ORDER: CaseField[] = ["title", "case_type", "description", "roles", "preconditions", "steps",
  "expected_behaviour", "source_chunk_ids"];

// One field, old (red) vs proposed (green). Lists mark removed / added items individually.
export function DiffField({ field, before, after, label }: {
  field: CaseField; before: unknown; after: unknown; label: (cid: number) => string;
}) {
  const fmt = (v: unknown) => (field === "source_chunk_ids" ? label(Number(v)) : String(v ?? ""));
  if (Array.isArray(before) || Array.isArray(after)) {
    const b = (before as unknown[]) || [], a = (after as unknown[]) || [];
    const bs = new Set(b.map(String)), as = new Set(a.map(String));
    const Tag = field === "steps" ? "ol" : "ul";
    return (
      <div className="uc-diff-cols">
        <Tag className="uc-diff-old">{b.map((x, i) => <li key={i} className={as.has(String(x)) ? "" : "del"}>{fmt(x)}</li>)}
          {b.length === 0 && <li className="none">—</li>}</Tag>
        <Tag className="uc-diff-new">{a.map((x, i) => <li key={i} className={bs.has(String(x)) ? "" : "add"}>{fmt(x)}</li>)}
          {a.length === 0 && <li className="none">—</li>}</Tag>
      </div>
    );
  }
  return (
    <div className="uc-diff-cols">
      <div className="uc-diff-old"><span className="del">{fmt(before) || "—"}</span></div>
      <div className="uc-diff-new"><span className="add">{fmt(after) || "—"}</span></div>
    </div>
  );
}
