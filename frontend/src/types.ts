export interface Project {
  project: string;
  title: string;
  docs: number;
  status?: "processing" | "ready" | "failed";
  progress?: number;      // 0–100 while processing
  eta_seconds?: number;   // approx time remaining while processing
  revision_no?: number;   // how many versions have been applied
  has_source?: boolean;   // the original file is stored (re-processable / downloadable)
  source_id?: number | null;   // id for GET /brd/source/{id} — set together with has_source
  error?: string | null;  // why processing failed
  job?: IngestJobState;   // latest ingestion job (new BRD or new version)
  review?: { id: number; summary: RevisionSummary } | null;   // a new version awaiting review
}

export interface RevisionSummary { same: number; modified: number; added: number; removed: number }
export interface IngestJobState {
  id: number; mode: "new" | "revision"; status: "queued" | "running" | "review" | "done" | "failed" | "canceled";
  stage: string | null; done: number; total: number | null; error: string | null; retry_at: string | null;
  warnings: string[];   // e.g. scanned pages that weren't searchable — non-fatal, shown alongside the job
}

export interface ConversationMeta {
  id: number;
  created_at: string;
  project: string | null;
  preview: string | null;
}

export interface Source {
  n: number;
  chunk_id?: number | null;
  req_id: string | null;
  section: string | null;
  project: string;
  snippet: string | null;
  full: string | null;
  removed?: boolean;      // the cited requirement was later removed (archived) from the BRD
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
