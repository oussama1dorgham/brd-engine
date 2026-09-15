// Friendly citation labels + clean evidence snippets, shared by chat sources and
// use-case traceability tags. Goal: never surface an internal chunk id, and never dump
// a raw multi-KB chunk — show a short human gist, cleanly trimmed.

export function cleanText(s: string | null | undefined): string {
  return (s || "").replace(/\s+/g, " ").trim();
}

// A readable gist of some text, whitespace-collapsed and trimmed with an ellipsis.
export function snippet(text: string | null | undefined, max = 180): string {
  const c = cleanText(text);
  return c.length > max ? c.slice(0, max).trimEnd() + "…" : c;
}

// A short human label for a requirement/source. Prefers a real requirement id or
// section; otherwise a short gist of the text; NEVER the internal chunk id.
export function reqLabel(r: {
  req_id?: string | null;
  section?: string | null;
  text?: string | null;
  ordinal?: number | null;
}): string {
  return (
    r.req_id ||
    r.section ||
    snippet(r.text, 32) ||
    (r.ordinal != null ? `Requirement ${r.ordinal + 1}` : "Requirement")
  );
}
