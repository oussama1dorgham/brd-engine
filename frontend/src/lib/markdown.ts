// Tiny, safe markdown renderer — escapes first, then formats a known subset.
// Ported verbatim from the original stdlib UI so answers render identically.

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!));
}

function mdInline(s: string): string {
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/\[(\d+)\]/g, '<span class="cite">[$1]</span>');
  return s;
}

function isSep(l: string): boolean {
  return /^\s*\|?\s*:?-{2,}/.test(l) && l.includes("-");
}

function cells(l: string): string[] {
  const c = l.split("|").map((x) => x.trim());
  if (c.length && c[0] === "") c.shift();
  if (c.length && c[c.length - 1] === "") c.pop();
  return c;
}

export function mdToHtml(md: string): string {
  const L = escapeHtml(md).split(/\r?\n/);
  let out = "";
  let i = 0;
  while (i < L.length) {
    const line = L[i];
    if (/^\s*$/.test(line)) { i++; continue; }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { const lv = Math.min(h[1].length, 6); out += "<h" + lv + ">" + mdInline(h[2].trim()) + "</h" + lv + ">"; i++; continue; }
    if (line.includes("|") && i + 1 < L.length && isSep(L[i + 1])) {
      const head = cells(line); i += 2; const rows: string[][] = [];
      while (i < L.length && L[i].includes("|") && !/^\s*$/.test(L[i])) { rows.push(cells(L[i])); i++; }
      out += '<div class="tbl"><table><thead><tr>' + head.map((c) => '<th dir="auto">' + mdInline(c) + "</th>").join("") + "</tr></thead><tbody>";
      rows.forEach((r) => { out += "<tr>" + head.map((_, ci) => '<td dir="auto">' + mdInline(r[ci] || "") + "</td>").join("") + "</tr>"; });
      out += "</tbody></table></div>"; continue;
    }
    if (/^\s*[-*]\s+/.test(line)) { const it: string[] = []; while (i < L.length && /^\s*[-*]\s+/.test(L[i])) { it.push(mdInline(L[i].replace(/^\s*[-*]\s+/, ""))); i++; } out += "<ul>" + it.map((x) => "<li>" + x + "</li>").join("") + "</ul>"; continue; }
    if (/^\s*\d+\.\s+/.test(line)) { const it: string[] = []; while (i < L.length && /^\s*\d+\.\s+/.test(L[i])) { it.push(mdInline(L[i].replace(/^\s*\d+\.\s+/, ""))); i++; } out += "<ol>" + it.map((x) => "<li>" + x + "</li>").join("") + "</ol>"; continue; }
    if (/^\s*>\s?/.test(line)) { const bq: string[] = []; while (i < L.length && /^\s*>\s?/.test(L[i])) { bq.push(mdInline(L[i].replace(/^\s*>\s?/, ""))); i++; } out += "<blockquote>" + bq.join("<br>") + "</blockquote>"; continue; }
    const buf = [line]; i++;
    while (i < L.length && !/^\s*$/.test(L[i]) && !/^#{1,6}\s/.test(L[i]) && !/^\s*[-*]\s+/.test(L[i]) && !/^\s*\d+\.\s+/.test(L[i]) && !/^\s*>\s?/.test(L[i]) && !(L[i].includes("|") && i + 1 < L.length && isSep(L[i + 1]))) { buf.push(L[i]); i++; }
    out += "<p>" + buf.map(mdInline).join("<br>") + "</p>";
  }
  return out;
}

// Source-tag labels (short chip label + full hover text), ported from the original.
import type { Source } from "../types";

export function srcLabel(x: Source): string {
  return x.req_id || x.section || x.snippet || "chunk " + x.n;
}

export function srcFull(x: Source): string {
  const lab = x.req_id || x.section;
  const body = (lab ? lab + " — " : "") + (x.full || x.snippet || "");
  return (body.trim() || srcLabel(x)) + "  ·  " + x.project;
}

function attrEsc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Render an answer to HTML, injecting data-tip (full source text) into each [n] citation.
export function answerHtml(content: string, sources?: Source[]): string {
  let html = mdToHtml(content);
  if (sources && sources.length) {
    const byN: Record<string, string> = {};
    sources.forEach((x) => { byN[String(x.n)] = srcFull(x); });
    html = html.replace(/<span class="cite">\[(\d+)\]<\/span>/g, (m, n) =>
      byN[n] ? `<span class="cite" data-tip="${attrEsc(byN[n])}">[${n}]</span>` : m);
  }
  return html;
}
