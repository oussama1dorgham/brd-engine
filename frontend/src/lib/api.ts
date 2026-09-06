import type { AskEvent, AttachEvent, ConversationMeta, Message, Project } from "../types";

async function jget<T>(url: string): Promise<T> {
  const r = await fetch(url);
  return r.json() as Promise<T>;
}

async function jpost<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json() as Promise<T>;
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
export interface LlmKeyMeta { available: boolean; configured: boolean; base_url?: string; masked?: string }
export const getLlmKey = () => jget<LlmKeyMeta>("/llm/key");
// `all` = every model the key can reach (the modal); `preferred` = curated subset (the picker).
export const getLlmModels = () =>
  jget<{ models: string[]; preferred: string[] }>("/llm/models")
    .then((d) => ({ all: d.models || [], preferred: d.preferred || [] }));
export const setPreferredModels = (models: string[]) =>
  jpost<{ ok?: boolean; preferred?: string[]; error?: string }>("/llm/preferred", { models });
export const deleteLlmKey = () => fetch("/llm/key", { method: "DELETE" });
export async function setLlmKey(base_url: string, api_key: string): Promise<{ masked: string; base_url: string; models: string[] }> {
  const r = await fetch("/llm/key", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ base_url, api_key }),
  });
  const d = await r.json().catch(() => ({ error: "bad server response" }));
  if (!r.ok || (d as { error?: string }).error) throw new Error((d as { error?: string }).error || "HTTP " + r.status);
  return d as { masked: string; base_url: string; models: string[] };
}

export const getProjects = () => jget<{ projects: Project[] }>("/projects").then((d) => d.projects || []);
export const getConversations = () => jget<{ conversations: ConversationMeta[] }>("/conversations").then((d) => d.conversations || []);
export const getMessages = (cid: number) => jget<{ messages: Message[] }>("/conversation/" + cid).then((d) => d.messages || []);

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

export const deleteBrd = (project: string) =>
  jpost<{ ok?: boolean; removed?: number; error?: string }>("/delete_brd", { project });
export const renameBrd = (project: string, title: string) =>
  jpost<{ ok?: boolean; title?: string; error?: string }>("/rename_brd", { project, title });

export async function uploadBrd(file: File, title: string): Promise<{ ok?: boolean; project?: string; title?: string; status?: string; error?: string }> {
  const headers: Record<string, string> = { "X-Filename": encodeURIComponent(file.name) };
  const nm = title.trim();
  if (nm) headers["X-Title"] = encodeURIComponent(nm);
  const r = await fetch("/upload", { method: "POST", headers, body: file });
  const d = await r.json().catch(() => ({ error: "bad server response" }));
  if (!r.ok || d.error) throw new Error(d.error || "HTTP " + r.status);
  return d;
}

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
