export function etaText(seconds?: number): string | null {
  if (seconds == null || seconds <= 0) return null;
  if (seconds < 60) return `~${Math.max(1, Math.round(seconds))}s left`;
  const m = Math.round(seconds / 60);
  if (m < 60) return `~${m}m left`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm ? `~${h}h ${mm}m left` : `~${h}h left`;
}
