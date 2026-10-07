import type { AskEvent, AttachEvent, ConversationMeta, Message, Project } from "../types";

// Rejects (throws an Error with a human-readable message) on a network failure or a
// non-JSON response body. A JSON body — on ANY HTTP status — still resolves as-is, unchanged:
// callers rely on fields like `stale`, `confirm_required`, `error` regardless of status code.
// Every caller that polls or feeds straight into render MUST `.catch()` (most already do).
async function jget<T>(url: string): Promise<T> {
  let r: Response;
  try {
    r = await fetch(url);
  } catch {
    throw new Error("Network error — check your connection and try again.");
  }
  try {
    return (await r.json()) as T;
  } catch {
    throw new Error(r.statusText || `Request failed (HTTP ${r.status})`);
  }
}

async function jpost<T>(url: string, body: unknown, headers?: Record<string, string>): Promise<T> {
  let r: Response;
  try {
    r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(headers || {}) },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error("Network error — check your connection and try again.");
  }
  try {
    return (await r.json()) as T;
  } catch {
    throw new Error(r.statusText || `Request failed (HTTP ${r.status})`);
  }
}

// --- auth ---
export interface AuthUser { id: number; email: string; email_verified: boolean }
export type OtpKind = "signup" | "login" | "reset";
export interface OtpStage { stage: "otp"; kind: OtpKind; email: string }
export type AuthResult = AuthUser | OtpStage;

export const isOtpStage = (r: AuthResult): r is OtpStage => (r as OtpStage).stage === "otp";

export const getMe = () =>
  fetch("/auth/me").then((r) => (r.ok ? (r.json() as Promise<AuthUser>) : null)).catch(() => null);

async function authPost(url: string, payload: unknown): Promise<AuthResult> {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await r.json().catch(() => ({ error: "bad server response" }));
  if (!r.ok || (d as { error?: string }).error) throw new Error((d as { error?: string }).error || "HTTP " + r.status);
  return d as AuthResult;
}

// login/signup may return a logged-in user OR an { stage: "otp" } challenge.
export const login = (email: string, password: string) => authPost("/auth/login", { email, password });
export const signup = (email: string, password: string) => authPost("/auth/signup", { email, password });
// forgot always returns an OTP stage (doesn't reveal whether the email exists).
export const forgotPassword = (email: string) =>
  authPost("/auth/forgot", { email }) as Promise<OtpStage>;
// these always resolve to a logged-in user on success.
export const verifyOtp = (email: string, code: string, kind: OtpKind) =>
  authPost("/auth/otp/verify", { email, code, kind }) as Promise<AuthUser>;
export const resetPassword = (email: string, code: string, password: string) =>
  authPost("/auth/reset", { email, code, password }) as Promise<AuthUser>;
export const resendOtp = (email: string, kind: OtpKind) =>
  fetch("/auth/otp/resend", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, kind }) });
export const logout = () => fetch("/auth/logout", { method: "POST" });

export async function changePassword(current_password: string, new_password: string): Promise<void> {
  const r = await fetch("/auth/change-password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current_password, new_password }),
  });
  const d = await r.json().catch(() => ({ error: "bad server response" }));
  if (!r.ok || (d as { error?: string }).error) throw new Error((d as { error?: string }).error || "HTTP " + r.status);
}
// LEGACY (link-based email verification) — kept for a future link mode; not in the active flow.
export const resendVerification = () => fetch("/auth/resend-verification", { method: "POST" });

// --- custom LLM (bring-your-own-key) ---
export interface LlmKeyMeta { available: boolean; configured: boolean; provider?: string; base_url?: string; masked?: string; label?: string | null }
export interface LlmKey { id: number; provider: string; base_url: string; masked: string; is_active: boolean; label: string | null }
export interface ProviderMeta { key: string; label: string; needs_base_url: boolean; default_base_url: string | null }
export const getLlmProviders = () =>
  jget<{ enabled: boolean; providers: ProviderMeta[] }>("/llm/providers");
// active-key summary (drives the app/chat); getLlmKeys is the full list (settings).
export const getLlmKey = () => jget<LlmKeyMeta>("/llm/key");
export const getLlmKeys = () => jget<{ available: boolean; keys: LlmKey[] }>("/llm/keys");
// `all` = every model the active key can reach (the modal); `preferred` = curated subset (the picker).
export const getLlmModels = () =>
  jget<{ models: string[]; preferred: string[] }>("/llm/models")
    .then((d) => ({ all: d.models || [], preferred: d.preferred || [] }));
export const setPreferredModels = (models: string[], key_id?: number) =>
  jpost<{ ok?: boolean; preferred?: string[]; error?: string }>("/llm/preferred", { models, key_id });
export const setActiveKey = (id: number) =>
  jpost<{ ok?: boolean; active?: number; error?: string }>("/llm/keys/active", { id });
export const deleteLlmKey = (id: number) =>
  jpost<{ ok?: boolean; error?: string }>("/llm/key/delete", { id });
export async function addLlmKey(provider: string, base_url: string, api_key: string, label = ""): Promise<{ id: number; masked: string; base_url: string; models: string[] }> {
  const r = await fetch("/llm/key", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider, base_url, api_key, label }),
  });
  const d = await r.json().catch(() => ({ error: "bad server response" }));
  if (!r.ok || (d as { error?: string }).error) throw new Error((d as { error?: string }).error || "HTTP " + r.status);
  return d as { id: number; masked: string; base_url: string; models: string[] };
}

export const getProjects = () => jget<{ projects: Project[] }>("/projects").then((d) => d.projects || []);

// --- service accounts / API tokens (external access) ---
export interface ApiToken {
  id: number; masked: string; scopes: string[]; rate_limit_per_min: number;
  expires_at: string | null; revoked_at: string | null; last_used_at: string | null; created_at: string;
}
export interface ServiceAccount {
  id: number; name: string; disabled: boolean; created_at: string; grants: string[]; tokens: ApiToken[];
}
export const getServiceAccounts = () =>
  jget<{ accounts: ServiceAccount[] }>("/service-accounts").then((d) => d.accounts || []);
export const createServiceAccount = (name: string) =>
  jpost<{ id?: number; error?: string }>("/service-accounts", { name });
export const deleteServiceAccount = (id: number) =>
  jpost<{ ok?: boolean; error?: string }>(`/service-accounts/${id}/delete`, {});
export const setServiceAccountDisabled = (id: number, disabled: boolean) =>
  jpost<{ ok?: boolean; error?: string }>(`/service-accounts/${id}/disable`, { disabled });
export const grantProject = (id: number, project: string) =>
  jpost<{ ok?: boolean; grants?: string[]; error?: string }>(`/service-accounts/${id}/grant`, { project });
export const revokeGrant = (id: number, project: string) =>
  jpost<{ ok?: boolean; grants?: string[]; error?: string }>(`/service-accounts/${id}/revoke-grant`, { project });
export const issueToken = (id: number, scopes: string[], rate_limit_per_min: number, expires_days: number | null) =>
  jpost<{ id?: number; token?: string; masked?: string; scopes?: string[]; error?: string }>(
    `/service-accounts/${id}/tokens`, { scopes, rate_limit_per_min, expires_days });
export const revokeToken = (tokenId: number) =>
  jpost<{ ok?: boolean; error?: string }>(`/tokens/${tokenId}/revoke`, {});
// The token-accessible endpoints, derived live from the backend routes (never hardcoded).
export interface TokenEndpoint { scope: string; method: string; path: string; title: string }
export const getTokenEndpoints = () =>
  jget<{ endpoints: TokenEndpoint[] }>("/token-endpoints").then((d) => d.endpoints || []);

// --- use cases (generated from a BRD, organized as a folder tree) ---
export interface UseCase {
  id: number; uid: number; folder_id: number; uc_id: string | null; title: string; description: string;
  roles: string[]; preconditions: string; steps: string[]; expected_behaviour: string;
  source_chunk_ids: number[]; ordinal: number; status: string;
  case_type: "functional" | "negative" | "edge";   // test-case angle
  test_status: TestStatus;                          // execution verdict (orthogonal to `status`)
  in_scope: boolean;                                // scope axis; out-of-scope = excluded from coverage
  needs_retest: boolean;                            // DERIVED: last verdict predates the current definition
  last_by: string | null; last_at: string | null; last_summary: string | null;   // latest audit entry
}
export interface UseCaseFolder {
  id: number; parent_id: number | null; name: string; ordinal: number;
  children: UseCaseFolder[]; use_cases: UseCase[];
}
// Ledger-derived resume state (survives a server restart): how many of the
// deterministic batches are already done vs still pending, so the UI can offer
// Resume (continue where a quota-stopped run left off) vs Regenerate.
export interface UcResumeState {
  exists: boolean; total: number; done: number; failed: number;
  pending: number; complete: boolean; resumable: boolean;
  superseded?: number; stale?: boolean;   // requirements added/changed/removed since generation → an Update is due
  changed?: number; added?: number; removed?: number;   // …per kind (added = never generated from yet)
}
export interface UseCaseStatus {
  running: boolean; idle?: boolean; done?: number; total?: number;
  scope?: string | null; made?: number; error?: string | null;
  batches?: number; skipped?: number; mode?: string;
  errors?: { scope: string; error: string }[];
  resume?: UcResumeState;
}
export const getUseCases = (project: string) =>
  jget<{ project: string; folders: UseCaseFolder[]; error?: string }>("/use-cases?project=" + encodeURIComponent(project));
// mode: "resume" (default — skip batches already written) | "replace" (wipe + regenerate).
// coverage: "functional" (happy path only) | "full" (also derive negative + edge cases).
export const generateUseCases = (
  project: string, replace = false, model: string | null = null, mode: string | null = null,
  coverage: string | null = null,
) => jpost<{ started?: boolean; error?: string }>("/use-cases/generate", { project, replace, model, mode, coverage });
export const getUseCaseStatus = (project: string) =>
  jget<UseCaseStatus>("/use-cases/status?project=" + encodeURIComponent(project));
// editing (QA refines the tree)
export type UseCasePatch = Partial<Pick<UseCase, "title" | "description" | "roles" | "preconditions" | "steps" | "expected_behaviour" | "case_type">>;
export const updateUseCase = (id: number, patch: UseCasePatch) =>
  jpost<{ ok?: boolean; error?: string }>("/use-cases/update", { id, ...patch });
export const deleteUseCase = (id: number) =>
  jpost<{ ok?: boolean; error?: string }>("/use-cases/delete", { id });
export const moveUseCase = (id: number, folder_id: number) =>
  jpost<{ ok?: boolean; error?: string }>("/use-cases/move", { id, folder_id });
export const createUcFolder = (project: string, parent_id: number | null, name: string) =>
  jpost<{ ok?: boolean; folder?: { id: number }; error?: string }>("/use-cases/folder/create", { project, parent_id, name });
export const renameUcFolder = (folder_id: number, name: string) =>
  jpost<{ ok?: boolean; error?: string }>("/use-cases/folder/rename", { folder_id, name });
export const deleteUcFolder = (folder_id: number) =>
  jpost<{ ok?: boolean; error?: string }>("/use-cases/folder/delete", { folder_id });

// --- use-case TEST EXECUTION (see docs/USECASE_TESTING.md) ---
// ONE ordered execution lifecycle. `retest` is DERIVED (a badge), `scope` is a SEPARATE axis —
// neither is a status here. Every write returns the event + the card's new state (no refetch).
export type TestStatus = "not_tested" | "in_progress" | "blocked" | "failed" | "passed";
export const TEST_STATUSES: TestStatus[] = ["not_tested", "in_progress", "blocked", "failed", "passed"];
export const TEST_STATUS_LABEL: Record<TestStatus, string> = {
  not_tested: "Not run", in_progress: "In progress", blocked: "Blocked",
  failed: "Failed", passed: "Passed",
};
// keyboard shortcut per verdict (also shown in the verdict menu)
export const TEST_STATUS_KEY: Partial<Record<TestStatus, string>> = {
  passed: "P", failed: "F", blocked: "B", in_progress: "I",
};
export const needsReason = (s: TestStatus) => s === "failed" || s === "blocked";
export interface UcTestProof {
  id: number; result_id: number | null;   // null = STAGED (claimed by the next verdict/note)
  kind: "image" | "video" | "file" | "link"; filename: string | null;
  mime: string | null; size: number | null; url: string | null; caption: string | null;
  ordinal: number; uploaded_at: string;
}
export interface UcTestEvent {
  result_id: number; event_kind: "status" | "note" | "scope"; status: TestStatus; note: string | null;
  in_scope: boolean | null;   // only meaningful on a 'scope' event: the value scope was set TO
  tested_against_ver: number | null; tested_at: string; tested_by: string | null;
  defect_ref: string | null; cycle_id: number | null; cycle_name: string | null;
  amended_from: TestStatus | null; amended_at: string | null;
  stale: boolean;        // a verdict the test definition has since moved past
  latest: boolean; undoable: boolean;
  proofs: UcTestProof[];
  carried_from?: number | null;   // a pass carried into a new cycle unchanged (points at the original result)
}
export interface UcCycleState {
  cycle_id: number; in_cycle: boolean; status: TestStatus; needs_retest: boolean;
  version?: number | null;        // the version the cycle tests
  in_version?: boolean;           // false: added to the draft after that version — can't be recorded here
  draft_differs?: boolean;        // the draft changed after that version — test the version's definition
}
export interface UcCardState {
  id: number; uid: number; test_status: TestStatus; in_scope: boolean; needs_retest: boolean;
  cycle?: UcCycleState;
  last_by?: string | null; last_at?: string | null; last_summary?: string | null;   // latest audit entry
}
export interface UcTimeline {
  uid: number; live: boolean; card: UcCardState | null; current_version: number;
  amend_window_seconds: number; pending: UcTestProof[]; events: UcTestEvent[];
}
export interface UcWrite { ok?: boolean; error?: string; event?: UcTestEvent | null; card?: UcCardState | null;
  amended?: boolean; claimed?: number }
export const getUseCaseTimeline = (uid: number, cycleId?: number | null) =>
  jget<UcTimeline>(`/use-cases/${uid}/timeline` + (cycleId ? `?cycle_id=${cycleId}` : ""));
// event_kind "status" records a verdict; "note" logs a comment/evidence without changing it.
export const recordTest = (id: number, p: {
  status?: TestStatus; note?: string; event_kind?: "status" | "note"; defect_ref?: string; cycle_id?: number | null;
}) => jpost<UcWrite>("/use-cases/test-status", {
  id, status: p.status ?? null, note: p.note || null, event_kind: p.event_kind || "status",
  defect_ref: p.defect_ref || null, cycle_id: p.cycle_id ?? null,
});
export const recordTestBulk = (ids: number[], status: TestStatus, cycle_id?: number | null) =>
  jpost<{ ok?: boolean; cards?: UcCardState[]; error?: string }>("/use-cases/test-status/bulk",
    { ids, status, cycle_id: cycle_id ?? null });
export const undoTest = (result_id: number, cycle_id?: number | null) =>
  jpost<{ ok?: boolean; card?: UcCardState | null; restored_note?: string | null; reverted?: boolean; error?: string }>(
    "/use-cases/test-status/undo", { result_id, cycle_id: cycle_id ?? null });
export const editTestEvent = (result_id: number, patch: { note?: string; defect_ref?: string }) =>
  jpost<{ ok?: boolean; event?: UcTestEvent | null; error?: string }>("/use-cases/test-status/edit", { result_id, ...patch });
// Scope axis (independent of the verdict): out-of-scope cards are excluded from coverage.
export const setScope = (id: number, in_scope: boolean, note?: string, cycle_id?: number | null) =>
  jpost<UcWrite>("/use-cases/scope", { id, in_scope, note: note || null, cycle_id: cycle_id ?? null });
export const setScopeBulk = (ids: number[], in_scope: boolean) =>
  jpost<{ ok?: boolean; cards?: UcCardState[]; error?: string }>("/use-cases/scope/bulk", { ids, in_scope });
export const stageProofLink = (id: number, url: string, caption?: string) =>
  jpost<{ ok?: boolean; proof?: UcTestProof; error?: string }>("/use-cases/proof/link", { id, url, caption: caption || null });
// File proof: raw body + metadata in headers (mirrors /upload). XHR (not fetch) for upload progress.
export function uploadProofFile(
  id: number, file: File, onProgress?: (fraction: number) => void,
): Promise<{ ok?: boolean; proof?: UcTestProof; error?: string }> {
  return new Promise((resolve) => {
    const x = new XMLHttpRequest();
    x.open("POST", "/use-cases/proof/file");
    x.setRequestHeader("X-UC-Id", String(id));
    x.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    x.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    if (onProgress) x.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    x.onload = () => { try { resolve(JSON.parse(x.responseText)); } catch { resolve({ error: "bad server response" }); } };
    x.onerror = () => resolve({ error: "upload failed — check your connection" });
    x.send(file);
  });
}
export const deleteProof = (proof_id: number) =>
  jpost<{ ok?: boolean; error?: string }>("/use-cases/proof/delete", { proof_id });
export const setProofCaption = (proof_id: number, caption: string) =>
  jpost<{ ok?: boolean; error?: string }>("/use-cases/proof/caption", { proof_id, caption });
export const proofUrl = (proof_id: number, download = false) =>
  `/use-cases/proof/${proof_id}` + (download ? "?download=1" : "");

// --- test cycles (named runs; traceability) ---
export interface CycleStats {
  total: number; executed: number; needs_retest: number; out_of_scope: number; carried?: number;
  by_status: Partial<Record<TestStatus, number>>;
}
export interface TestCycle {
  id: number; name: string; description: string | null; status: "open" | "closed";
  created_at: string; closed_at: string | null; stats: CycleStats;
  version?: { id: number; number: number; label: string | null } | null;   // the published version it tests
  latest_version?: number | null; outdated?: boolean;                        // a newer version exists
  carried?: number; unpublished_excluded?: number;                           // (on create)
}
export interface CycleCase {
  status: TestStatus; needs_retest: boolean; tested_at: string | null; defect_ref: string | null;
  draft_differs?: boolean; carried?: boolean;
}
export type CycleSource = "all" | "not_passed" | "failed_blocked" | "retest" | "empty";
export const getTestCycles = (project: string) =>
  jget<{ cycles?: TestCycle[]; error?: string }>("/test-cycles?project=" + encodeURIComponent(project))
    .then((d) => d.cycles || []);
export const createTestCycle = (p: {
  project: string; name: string; description?: string; source: CycleSource;
  case_types?: string[]; folder_ids?: number[]; from_cycle_id?: number | null;
  carry?: boolean;                // carry passes of cases unchanged since they passed (default on)
}) => jpost<{ ok?: boolean; cycle?: TestCycle; error?: string }>("/test-cycles/create", p);
export const updateTestCycle = (cycle_id: number, patch: { name?: string; description?: string; status?: "open" | "closed" }) =>
  jpost<{ ok?: boolean; cycle?: TestCycle; error?: string }>("/test-cycles/update", { cycle_id, ...patch });
export const deleteTestCycle = (cycle_id: number) =>
  jpost<{ ok?: boolean; error?: string }>("/test-cycles/delete", { cycle_id });
export const setCycleMembers = (cycle_id: number, add_ids: number[], remove_uids: number[] = []) =>
  jpost<{ ok?: boolean; stats?: CycleStats; error?: string }>("/test-cycles/members", { cycle_id, add_ids, remove_uids });
export const getCycleStatuses = (cycle_id: number) =>
  jget<{ cycle?: TestCycle; cases?: Record<string, CycleCase>; error?: string }>(`/test-cycles/${cycle_id}/statuses`);

// --- coverage + traceability report ---
export type Coverage = "uncovered" | "failed" | "blocked" | "retest" | "passed" | "not_run" | "partial";
export interface UcReportCase {
  id: number | null;               // null: deleted from the draft since the run (a versioned report still shows it)
  uid: number; uc_id: string | null; title: string; case_type: string; folder: string;
  status: TestStatus; needs_retest: boolean; tested_at: string | null; tested_by: string | null;
  defect_ref: string | null; note: string | null; proofs: number; source_chunk_ids: number[];
  carried?: boolean; live?: boolean;
}
export interface UcReportRequirement {
  chunk_id: number; req_id: string | null; section: string | null; ordinal: number | null; text: string;
  coverage: Coverage; counts: Partial<Record<TestStatus, number>>; case_uids: number[];
  changed_since_version?: boolean; removed_since_version?: boolean;   // text shown is the version's
}
export interface UcReport {
  project: string; cycle: TestCycle | null; total: number; executed: number; out_of_scope: number;
  needs_retest: number; by_status: Partial<Record<TestStatus, number>>;
  by_case_type: Record<string, Partial<Record<TestStatus, number>>>;
  pass_rate: number | null; progress: number | null; coverage: Partial<Record<Coverage, number>>;
  requirements: UcReportRequirement[]; cases: UcReportCase[];
  defects: Pick<UcReportCase, "id" | "uid" | "uc_id" | "title" | "status" | "defect_ref" | "note" | "tested_at" | "needs_retest">[];
  carried?: number;
  version?: { id: number; number: number; label: string | null; brd_revision_no?: number | null } | null;
  changes_since_previous?: {
    base: number; summary: SuiteSummary;
    items: { uid: number; uc_id: string | null; title: string | null; case_type: string | null;
             change: SuiteChange; fields: string[]; moved: boolean; retest: boolean }[];
  } | null;
  draft_changes?: SuiteSummary | null;      // overall report: what the draft changes vs the latest version
  error?: string;
}
// --- run-over-run comparison ---
export type RunChange = "regressed" | "new_failure" | "still_failing" | "fixed" | "newly_passing" | "not_rerun"
  | "dropped" | "not_run" | "still_passing";
export interface RunCompareItem {
  uid: number; id: number | null; uc_id: string | null; title: string | null; case_type: string | null;
  base_status: TestStatus | null; target_status: TestStatus | null; change: RunChange; carried: boolean;
  defect_ref: string | null; definition: string | null; definition_fields: string[]; retest: boolean;
}
export interface RunCompare {
  base: TestCycle; target: TestCycle; summary: Record<RunChange, number>; items: RunCompareItem[];
  definition_changes: SuiteSummary | null; error?: string;
}
// --- live usage: model keys' free allowance (OpenRouter) + embedding capacity (our Voyage ledger) ---
export interface KeyUsage {
  available: boolean; provider: string; error?: string; checked_at?: string; is_free_tier?: boolean;
  free_daily: { used: number | null; limit: number | null; remaining: number | null } | null;
  credits: { used_total: number | null; used_today: number | null; limit: number | null; remaining: number | null };
}
export interface UsageStatus {
  uses: "own" | "system"; own_key: KeyUsage | null; system: KeyUsage | null;
  embeddings: { provider: string; scope: string; rpm_limit: number; rpm_used: number; tpm_limit: number; tpm_used: number;
                reserved_for_chat: number; queries_waiting: number; blocked_for: number; window_resets_in: number };
  error?: string;
}
export const getUsage = (refresh = false) => jget<UsageStatus>("/usage" + (refresh ? "?refresh=true" : ""));
export const compareCycles = (project: string, base: number, target: number) =>
  jget<RunCompare>(`/test-cycles/compare?project=${encodeURIComponent(project)}&base=${base}&target=${target}`);
// --- per-use-case AUDIT LOG (append-only: who / what / when / status before → after) ---
export type AuditCategory = "definition" | "test" | "evidence" | "scope" | "cycle" | "proposal" | "version";
export interface UcAuditEntry {
  id: number; at: string; actor: string | null; source: "user" | "regeneration";
  category: AuditCategory; action: string; summary: string | null;
  status_before: TestStatus | null; status_after: TestStatus | null;
  cycle_id: number | null; cycle_name: string | null; version_no: number | null; result_id: number | null;
  detail: { note?: string; defect_ref?: string; attachments?: number; note_before?: string; bulk?: number;
            kind?: string; reason?: string; [k: string]: unknown };
  uc_id: string | null;
  suite_version?: number | null;          // the published version this entry happened in
}
export const getUseCaseAudit = (uid: number) =>
  jget<{ uid: number; counts: Partial<Record<AuditCategory, number>>; entries: UcAuditEntry[] }>(`/use-cases/${uid}/audit`);


// --- suite versions: the draft (live cases) is published as immutable, numbered versions ---
export interface SuiteVersion {
  id: number; number: number; status: "published" | "archived"; source: string; label: string | null;
  brd_revision_no: number | null; counts: { cases?: number; functional?: number; negative?: number; edge?: number };
  created_by: string | null; created_at: string; runs: number;
}
export type SuiteChange = "added" | "removed" | "modified" | "requirement_changed" | "moved" | "reordered";
export interface SuiteSummary {
  added: number; removed: number; modified: number; requirement_changed: number; moved: number;
  reordered: number; unchanged: number; retest: number; has_changes: boolean;
}
export interface UcCaseDef {
  uc_id?: string | null; title?: string; description?: string; case_type?: string; roles?: unknown;
  preconditions?: string; steps?: unknown; expected_behaviour?: string; source_chunk_ids?: unknown;
}
export interface SuiteDiffItem {
  uid: number; uc_id: string | null; title: string | null; case_type: string | null; change: SuiteChange;
  fields: string[]; moved: boolean; retest: boolean;
  requirements: { chunk_id: number; before: string | null; after: string | null }[];
  before?: UcCaseDef; after?: UcCaseDef; folder_before?: string[]; folder_after?: string[];
}
export interface SuiteDiff {
  summary: SuiteSummary; items: SuiteDiffItem[]; base: SuiteVersion | null;
  target: SuiteVersion | "draft"; fingerprint?: string; error?: string;
}
export interface SuiteStatus {
  latest: SuiteVersion | null; draft: SuiteSummary; fingerprint: string; versions: SuiteVersion[]; error?: string;
  brd_revision_no?: number | null;         // the BRD's CURRENT revision (a version records the one it was built on)
}
export const getSuiteStatus = (project: string) =>
  jget<SuiteStatus>("/use-cases/versions?project=" + encodeURIComponent(project));
export const getSuiteDiff = (project: string, base: number | null, target: number | "draft" = "draft") =>
  jget<SuiteDiff>("/use-cases/versions/diff?project=" + encodeURIComponent(project)
    + (base != null ? `&base=${base}` : "") + `&target=${target}`);
export const publishSuite = (project: string, label: string, expect: string) =>
  jpost<{ ok?: boolean; created?: boolean; version?: SuiteVersion; summary?: SuiteSummary; error?: string }>(
    "/use-cases/versions/publish", { project, label, expect });
export interface VersionCase {
  version: number; uid: number; case: UcCaseDef; folder_path: string[];
  requirements: { chunk_id: number; text: string | null }[]; error?: string;
}
export const getVersionCase = (project: string, versionId: number, uid: number) =>
  jget<VersionCase>(`/use-cases/versions/${versionId}/case/${uid}?project=` + encodeURIComponent(project));
// Make the draft equal to a published version (review, then publish it as a new version).
export const restoreSuiteVersion = (project: string, version_id: number) =>
  jpost<{ ok?: boolean; version?: number; removed?: number; updated?: number; restored?: number;
          requirements_changed_since?: number; error?: string }>("/use-cases/versions/restore", { project, version_id });
export const labelSuiteVersion = (project: string, version_id: number, label: string) =>
  jpost<{ ok?: boolean; version?: SuiteVersion; error?: string }>("/use-cases/versions/label", { project, version_id, label });

export const getUseCaseReport = (project: string, cycleId?: number | null) =>
  jget<UcReport>("/use-cases/report?project=" + encodeURIComponent(project) + (cycleId ? `&cycle_id=${cycleId}` : ""));

// --- version history / audit / restore (backup) ---
export interface UcVersion {
  version_no: number; change_kind: string; change_summary: string | null;
  changed_at: string; changed_by: string | null; uc_id: string | null;
}
export interface UcDiffField { before: unknown; after: unknown }
export interface UcDiff {
  uid: number; version_no: number; prev_version_no: number | null;
  fields: Record<string, UcDiffField>; snapshot?: Record<string, unknown>; found: boolean;
}
export interface UcDeleted {
  uid: number; uc_id: string | null; version_no: number; change_kind: string;
  changed_at: string; title: string | null;
}
export const getUseCaseHistory = (uid: number) =>
  jget<{ uid: number; live: boolean; versions: UcVersion[] }>(`/use-cases/${uid}/history`);
export const getUseCaseDiff = (uid: number, versionNo: number) =>
  jget<UcDiff>(`/use-cases/${uid}/history/${versionNo}`);
export const restoreUseCase = (uid: number, version_no: number) =>
  jpost<{ restored?: boolean; error?: string }>("/use-cases/restore", { uid, version_no });
export const getDeletedUseCases = (project: string) =>
  jget<{ deleted: UcDeleted[]; error?: string }>("/use-cases/deleted?project=" + encodeURIComponent(project));
// Lazy-loaded page of conversations. `before` = the last id you've seen (keyset cursor).
export const getConversationsPage = (before?: number, limit = 30) => {
  const qs = new URLSearchParams({ limit: String(limit) });
  if (before) qs.set("before", String(before));
  return jget<{ conversations: ConversationMeta[]; has_more: boolean }>("/conversations?" + qs.toString())
    .then((d) => ({ conversations: d.conversations || [], hasMore: !!d.has_more }));
};
export const getMessages = (cid: number) =>
  jget<{ messages: Message[]; model: string | null }>("/conversation/" + cid)
    .then((d) => ({ messages: d.messages || [], model: d.model ?? null }));
// persist the picked model for a conversation immediately (without waiting for the next /ask)
export const setConversationModel = (cid: number, model: string) =>
  jpost<{ ok?: boolean; error?: string }>(`/conversation/${cid}/model`, { model });

// Re-attach to an answer still generating for a conversation (after refresh/return).
// Yields {idle} if nothing is running, else {question} then the token/done/error stream.
export async function* attachStream(cid: number, signal?: AbortSignal): AsyncGenerator<AttachEvent> {
  const r = await fetch("/conversation/" + cid + "/stream", { signal });
  const reader = r.body!.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl: number;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      try {
        yield JSON.parse(line) as AttachEvent;
      } catch {
        /* ignore malformed line */
      }
    }
  }
}
export const getStarters = (project: string) =>
  jget<{ questions: string[] }>("/starters?project=" + encodeURIComponent(project)).then((d) => d.questions || []).catch(() => []);

// Stop an in-flight answer server-side (worker bails, nothing persisted).
export const cancelAsk = (cid: number) =>
  fetch("/ask/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ conversation_id: cid }) }).catch(() => {});

export const newConversation = () => jpost<{ conversation_id: number }>("/new", {}).then((d) => d.conversation_id);
export const deleteConversation = (cid: number) => jpost("/delete", { conversation_id: cid });

// --- change a requirement (QA): edit / add / delete / revert + versioning ---
export interface Requirement {
  chunk_id: number; req_id: string | null; section: string | null; ordinal: number; text: string;
  archived?: boolean; retired_at?: string | null;   // present when fetched with includeArchived
}
export interface BrdVersion { id: number; label: string | null; kind: string; created_at: string; created_by: string | null; requirements: number }
export const getRequirements = (project: string, includeArchived = false) =>
  jget<{ requirements: Requirement[] }>("/brd/requirements?project=" + encodeURIComponent(project)
    + (includeArchived ? "&include_archived=1" : "")).then((d) => d.requirements || []);
// friendly, cached short titles for cited requirement chunks (used as citation labels)
export const getRequirementTitles = (project: string, chunk_ids: number[]) =>
  jpost<{ titles: Record<string, string> }>("/requirements/titles", { project, chunk_ids }).then((d) => d.titles || {});
export const updateRequirement = (chunk_id: number, new_text: string) =>
  jpost<{ ok?: boolean; chunks?: number; error?: string }>("/brd/requirement/update", { chunk_id, new_text });
export const addRequirement = (project: string, text: string, req_id?: string, after_chunk_id?: number) =>
  jpost<{ ok?: boolean; error?: string }>("/brd/requirement/add", { project, text, req_id: req_id || null, after_chunk_id: after_chunk_id ?? null });
export const deleteRequirement = (chunk_id: number) =>
  jpost<{ ok?: boolean; error?: string }>("/brd/requirement/delete", { chunk_id });
export const revertRequirement = (chunk_id: number) =>
  jpost<{ ok?: boolean; reverted?: boolean; error?: string }>("/brd/requirement/revert", { chunk_id });
// Propose row boundaries for a flattened table chunk (review before persisting).
export const proposeRowSplit = (chunk_id: number) =>
  jpost<{ ok?: boolean; flattened?: boolean; rows?: string[]; conserved?: boolean; error?: string }>("/brd/requirement/structure", { chunk_id });
export const splitRequirement = (chunk_id: number, rows: string[]) =>
  jpost<{ ok?: boolean; rows?: number; error?: string }>("/brd/requirement/split", { chunk_id, rows });
// Story-driven change: describe a change in plain words → grounded proposal → apply.
export interface ChangeEdit { find: string; replace: string }
export interface ChangeOp {
  op: "edit" | "add" | "delete";
  scope?: "primary" | "consistency";   // consistency = knock-on edit to a touched requirement
  chunk_id?: number;
  after_chunk_id?: number | null;
  req_id?: string | null;
  old_text?: string | null;
  new_text?: string;
  changes?: ChangeEdit[];   // precise find/replace spans (edit ops)
  label?: string | null;
  reason?: string;
}
export interface RelatedScope { chunk_id: number; label: string; text: string; reason: string }
export const planChange = (project: string, story: string, refine = true, model: string | null = null) =>
  jpost<{ ok?: boolean; refined?: string; operations?: ChangeOp[]; related?: RelatedScope[]; error?: string }>("/brd/requirement/plan", { project, story, refine, model });
export interface ApplyChangeMeta { story?: string; refined?: string; model?: string | null; idempotencyKey?: string }
export const applyChange = (project: string, operations: ChangeOp[], meta: ApplyChangeMeta = {}) =>
  jpost<{ ok?: boolean; applied?: number; edited?: number; added?: number; deleted?: number;
          change_id?: number; idempotent_replay?: boolean; conflict?: boolean; chunk_id?: number; error?: string }>(
    "/brd/requirement/apply",
    { project, operations, story: meta.story, refined: meta.refined, model: meta.model },
    meta.idempotencyKey ? { "Idempotency-Key": meta.idempotencyKey } : undefined,
  );
export interface ChangeRequestRow {
  id: number; story: string | null; refined: string | null; model: string | null;
  applied: number; created_at: string; author: string | null; changes: number;
}
export const listChangeRequests = (project: string) =>
  jget<{ changes: ChangeRequestRow[] }>("/brd/requirement/changes?project=" + encodeURIComponent(project));
// On-demand: ask the agent to propose a precise edit for one touched requirement.
export const scopeEdit = (chunk_id: number, change: string, model: string | null = null) =>
  jpost<{ ok?: boolean; op?: ChangeOp; none?: boolean; reason?: string; error?: string }>("/brd/requirement/scope_edit", { chunk_id, change, model });
export const getVersions = (project: string) =>
  jget<{ review_status: string; versions: BrdVersion[] }>("/brd/versions?project=" + encodeURIComponent(project));
export const snapshotVersion = (project: string, label: string) =>
  jpost<{ ok?: boolean; error?: string }>("/brd/version/snapshot", { project, label });
export const restoreVersion = (project: string, snapshot_id: number) =>
  jpost<{ ok?: boolean; error?: string }>("/brd/version/restore", { project, snapshot_id });
export const approveDraft = (project: string) =>
  jpost<{ ok?: boolean; error?: string }>("/brd/approve", { project });
export const discardDraft = (project: string) =>
  jpost<{ ok?: boolean; discarded?: boolean; error?: string }>("/brd/discard", { project });

export const deleteBrd = (project: string) =>
  jpost<{ ok?: boolean; removed?: number; error?: string }>("/delete_brd", { project });
export const renameBrd = (project: string, title: string) =>
  jpost<{ ok?: boolean; title?: string; error?: string }>("/rename_brd", { project, title });

// Upload a BRD. `project` = upload a NEW VERSION of that BRD (processed, compared, staged for
// review); `asNew` = keep it as a separate BRD when its title clashes with an existing one.
// A clash without either answers {conflict} so the UI can ask what the user intends.
export interface UploadResult {
  ok?: boolean; job_id?: number; project?: string; title?: string; mode?: "new" | "revision"; status?: string;
  unchanged?: boolean; message?: string; conflict?: { project: string; title: string }; error?: string;
}
export async function uploadBrd(file: File, title: string, opts: { project?: string; asNew?: boolean } = {}): Promise<UploadResult> {
  const headers: Record<string, string> = { "X-Filename": encodeURIComponent(file.name) };
  const nm = title.trim();
  if (nm) headers["X-Title"] = encodeURIComponent(nm);
  if (opts.project) headers["X-Project"] = encodeURIComponent(opts.project);
  if (opts.asNew) headers["X-As-New"] = "1";
  const r = await fetch("/upload", { method: "POST", headers, body: file });
  const d = await r.json().catch(() => ({ error: "bad server response" }));
  if (r.status === 409 && d.conflict) return d;          // the caller asks the user what they intend
  if (!r.ok || d.error) throw new Error(d.error || "HTTP " + r.status);
  return d;
}

// --- BRD new versions (staged revisions) + ingestion jobs ---
export type RevisionChange = "same" | "modified" | "added" | "removed";
export interface RevisionItem {
  id: number; change: RevisionChange; ordinal: number | null; live_chunk_id: number | null;
  req_id: string | null; section: string | null; section_path: string | null; page: number | null;
  text: string | null; old_text: string | null; similarity: number | null;
  use_cases: number; tested_use_cases: number;
}
export interface BrdRevision {
  id: number; document_id: number; created_at: string; base_revision_no: number; filename: string | null;
  source_id: number | null; summary: { same: number; modified: number; added: number; removed: number };
  items: RevisionItem[]; stale: boolean;
  needs_confirmation: boolean;   // server thinks this looks like a near/full replacement
  replace_ratio: number;         // fraction of the CURRENT requirements that still MATCH this
                                  // version (not the fraction that would be replaced — low means risky)
}
// GET returns the raw envelope (not pre-unwrapped) so the caller can tell "no revision pending"
// (revision: null, no error) apart from a server-side JSON error ({error} with revision absent).
// jget rejects outright on a network failure / non-JSON body — the caller must catch that too.
export const getRevision = (project: string) =>
  jget<{ revision?: BrdRevision | null; error?: string }>("/brd/revision?project=" + encodeURIComponent(project));
export interface RevisionConfirmSummary {
  same: number; modified: number; added: number; removed: number;
  live: number; matched: number; matched_ratio: number;
}
export const applyRevision = (revision_id: number, keep_removed: number[], skip: number[], confirm_replace = false) =>
  jpost<{ ok?: boolean; kept?: number; updated?: number; added?: number; revived?: number; archived?: number;
          stale?: boolean; confirm_required?: boolean; summary?: RevisionConfirmSummary; error?: string }>(
    "/brd/revision/apply", { revision_id, keep_removed, skip, confirm_replace });
export const discardRevision = (revision_id: number) =>
  jpost<{ ok?: boolean; error?: string }>("/brd/revision/discard", { revision_id });
export const recompareRevision = (revision_id: number) =>
  jpost<{ ok?: boolean; error?: string }>("/brd/revision/recompare", { revision_id });
export const retryIngestJob = (job_id: number) =>
  jpost<{ ok?: boolean; error?: string }>("/ingest/jobs/retry", { job_id });
export const cancelIngestJob = (job_id: number) =>
  jpost<{ ok?: boolean; error?: string }>("/ingest/jobs/cancel", { job_id });
// Re-process a BRD from its stored original file — produces a new revision to review, same as
// re-uploading the same file. `has_source` on the project gates whether this is offered.
export const reprocessBrd = (project: string) =>
  jpost<{ ok?: boolean; job_id?: number; error?: string }>("/ingest/reprocess", { project });
// Direct download link for the BRD's original uploaded file (session-cookie authed).
export const sourceDownloadUrl = (sourceId: number) => `/brd/source/${sourceId}`;
// The server's hard cap on an uploaded file's size (kept here as the single client-side mirror
// of backend/core/config.py's INGEST_MAX_BYTES, so the UI can reject an oversized pick before a
// slow upload even starts; the server still enforces its own limit regardless).
export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

// Stream the answer as newline-delimited JSON events: {t}* then {done} | {error}.
// Pass an AbortSignal to cancel the read when the user leaves/switches the chat.
export async function* askStream(question: string, conversationId: number | null, project: string | null, model?: string | null, signal?: AbortSignal): AsyncGenerator<AskEvent> {
  const r = await fetch("/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, conversation_id: conversationId, project, model: model || null }),
    signal,
  });
  if (r.status === 409) { yield { busy: true }; return; }  // a prior turn is still finishing
  const reader = r.body!.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl: number;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      try {
        yield JSON.parse(line) as AskEvent;
      } catch {
        /* ignore malformed line */
      }
    }
  }
}
