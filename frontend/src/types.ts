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
  queued?: boolean;   // a not-yet-sent prompt waiting for the current answer to finish
  canceled?: boolean; // the user hit Stop — turn was aborted, nothing persisted
}

export type AskEvent =
  | { t: string }
  | { done: true; conversation_id: number; answer: string; standalone: string; sources: Source[] }
  | { canceled: true }   // the turn was stopped by the user
  | { busy: true }       // 409: a turn is still finishing for this conversation — retry shortly
  | { error: string };

// The re-attach stream adds two control events on top of AskEvent:
//   {idle} = nothing is generating; {question} = the pending turn's question.
export type AttachEvent = AskEvent | { idle: true } | { question: string };
