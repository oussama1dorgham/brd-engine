import type { AskEvent, ConversationMeta, Message, Project } from "../types";

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

export const getProjects = () => jget<{ projects: Project[] }>("/projects").then((d) => d.projects || []);
export const getConversations = () => jget<{ conversations: ConversationMeta[] }>("/conversations").then((d) => d.conversations || []);
export const getMessages = (cid: number) => jget<{ messages: Message[] }>("/conversation?id=" + cid).then((d) => d.messages || []);
export const getStarters = (project: string) =>
  jget<{ questions: string[] }>("/starters?project=" + encodeURIComponent(project)).then((d) => d.questions || []).catch(() => []);

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
export async function* askStream(question: string, conversationId: number | null, project: string | null): AsyncGenerator<AskEvent> {
  const r = await fetch("/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, conversation_id: conversationId, project }),
  });
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
