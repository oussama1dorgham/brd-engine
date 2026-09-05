export interface Project {
  project: string;
  title: string;
  docs: number;
  status?: "processing" | "ready" | "failed";
  progress?: number;      // 0–100 while processing
  eta_seconds?: number;   // approx time remaining while processing
}

export interface ConversationMeta {
  id: number;
  created_at: string;
  project: string | null;
  preview: string | null;
}

export interface Source {
  n: number;
  req_id: string | null;
  section: string | null;
  project: string;
  snippet: string | null;
  full: string | null;
}

export interface Message {
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  cond?: string;      // "interpreted as: …" when the question was condensed
  streaming?: boolean;
  loading?: boolean;
  error?: boolean;
}

export type AskEvent =
  | { t: string }
  | { done: true; conversation_id: number; answer: string; standalone: string; sources: Source[] }
  | { error: string };
