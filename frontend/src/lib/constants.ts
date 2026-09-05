// The BRDS mascot: a brown bear with glasses. `wink` closes the right eye —
// used in the generation/loading messages so he winks while thinking.
const bearHead = (wink: boolean) =>
  '<svg viewBox="0 0 100 100" width="100%" height="100%" aria-hidden="true" xmlns="http://www.w3.org/2000/svg">' +
  '<circle cx="28" cy="30" r="15" fill="#8a5a2b"/><circle cx="72" cy="30" r="15" fill="#8a5a2b"/>' +
  '<circle cx="28" cy="30" r="7" fill="#c98f52"/><circle cx="72" cy="30" r="7" fill="#c98f52"/>' +
  '<ellipse cx="50" cy="55" rx="37" ry="34" fill="#b5793f"/>' +
  '<rect x="16" y="31" width="68" height="10" rx="4" fill="var(--bandana)"/>' +
  '<text x="50" y="39.3" text-anchor="middle" font-size="9" font-weight="800" fill="var(--bandana-ink)" ' +
  'font-family="ui-sans-serif,system-ui,sans-serif" letter-spacing="0.5">QA</text>' +
  '<ellipse cx="50" cy="66" rx="21" ry="16" fill="#f0dcbf"/>' +
  '<circle cx="37" cy="49" r="3.6" fill="#2a1c10"/>' +
  (wink
    ? '<path d="M57 50 Q63 54 69 50" fill="none" stroke="#2a1c10" stroke-width="3.2" stroke-linecap="round"/>'
    : '<circle cx="63" cy="49" r="3.6" fill="#2a1c10"/>') +
  '<g fill="none" stroke="#2b2b2b" stroke-width="3" stroke-linecap="round">' +
  '<circle cx="37" cy="49" r="11"/><circle cx="63" cy="49" r="11"/>' +
  '<path d="M48 48h4"/><path d="M26 45l-9-4"/><path d="M74 45l9-4"/></g>' +
  '<ellipse cx="50" cy="61" rx="5.5" ry="4" fill="#2a1c10"/>' +
  '<path d="M44 70 Q50 76 56 70" fill="none" stroke="#2a1c10" stroke-width="2.4" stroke-linecap="round"/>' +
  '</svg>';

export const LOGO_SVG = bearHead(false);
export const LOGO_WINK_SVG = bearHead(true);

export const LOADING = [
  "Smashing the BRD", "Crushing the BRD", "Crucifying the BRD", "Pulverizing the BRD",
  "Interrogating the BRD", "Grilling the BRD", "Dissecting the BRD", "Wrestling the BRD",
  "Shredding the BRD", "Ransacking the BRD", "Cross-examining the BRD", "Mining the BRD",
];

export const SUN_SVG =
  '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/></svg>';

export const MOON_SVG =
  '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
