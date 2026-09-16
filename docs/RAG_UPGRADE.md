# RAG Pipeline Upgrade — hybrid + rerank, pre-index query transformation, measurement

Working spec for branch `feat/rag-upgrade`.

## Goal

Upgrade retrieval to a fully automated, measured pipeline: hybrid search + reranking,
query transformation **before** the index, and a real retrieval/answer **quality harness**
so no answer is trusted before the numbers are. Keep the **full scope and all data
enhancements** (real BRDs, Arabic, tables).

## Hard constraints (non-negotiable)

1. **No resource upgrade** — stay within the **current free-tier resources** (Voyage
   3 rpm / 10k tpm, free OpenRouter models, the existing Postgres). DB and network calls
   are fine; what's off-limits is *requiring a paid tier bump or new paid infra*. Every
   added call must fit under the existing rate/token caps (throttle + batch + cache).
2. **Minimal latency** — added stages are local, cached, batched, or parallelised; no
   gratuitous serial round-trips.
3. **No scope cut** — Arabic, real corpora, multi-fact, tables, abstention all covered.

### Design rules to satisfy the above

- **Local-first transforms.** Arabic normalization, ID/acronym handling, dedup, MMR,
  fusion weighting, threshold calibration are pure code — no calls, ~microseconds.
- **Cache is the multiplier.** The embed cache is immutable (free on hit); the retrieve
  cache short-circuits embed+rerank. Multi-query/HyDE variants and eval reuse these, so
  steady-state stays under the caps. A single embed is reused across all eval stages.
- **Respect the caps.** Anything that fans out calls (multi-query embeds, eval
  generations, LLM judges) goes through the existing Voyage/OpenRouter throttles, is
  batched where the API allows, and is cached — so throughput never needs a paid tier.
- **Fold when cheap.** Prefer folding extra work into a call that already fires (e.g. the
  condense call) when it's free to do so, but this is an optimisation, not a hard limit.
- **Eval is offline + subset-gated.** CI runs a subset; full eval runs nightly. Answer
  groundedness reuses the existing verifier (`refine.py`) rather than adding a judge.

## Current state (baseline)

- Hybrid: pgvector cosine (top 20) + Postgres FTS `search_tsv` (top 20) → **RRF** →
  **Voyage rerank-2.5** (fused top 10 → top 5). Cached, owner/project scoped
  ([backend/retrieve/retriever.py](../backend/retrieve/retriever.py)).
- Query rewrite: **condense** follow-ups only ([backend/generate/condense.py](../backend/generate/condense.py)).
- Eval: 10 synthetic cases, recall@k + MRR, rerank on/off
  ([backend/eval/recall.py](../backend/eval/recall.py), [data/golden_qa.json](../data/golden_qa.json)).

## The dominating constraint

Real BRDs have **0 req_ids / sections** (0/335 chunks) and are **Arabic + flattened
tables**. So the harness must be **chunk_id-keyed** (not req_id) and multilingual, and FTS
(`simple` config, no Arabic handling) needs **query normalization** to compete.

## Phases

- [x] **Phase 0 — Measurement (first, zero-cost).** Chunk_id-keyed Golden Set v2 (real +
  Arabic); metrics recall@k, MRR, nDCG@k, context precision, abstention accuracy;
  per-stage attribution (vector / keyword / fused / +rerank / +transform); one
  `python -m backend.eval.run` → JSON + table; baseline report. Cached embeds, batched.
- [x] **Phase 1 — Query transformation (local + folded).** Arabic normalization +
  ID/acronym handling (local, free); keyword-arm expansion; fold richer expansion into the
  condense call. Measure each vs baseline.
- [x] **Phase 2 — Hybrid/rerank tuning.** Candidate depth, weighted RRF, MMR/dedup,
  abstention score threshold. Measure.
- [x] **Phase 3 — Answer-level eval + CI gate.** Groundedness/faithfulness (reuse
  `refine.py`), citation correctness; regression gate (subset per PR, full nightly).
- [ ] **Phase 4 (upstream, optional).** Finer chunking / passage rerank if metrics say so.

## Automation / flow

- Single entry point `python -m backend.eval.run [--full|--subset] [--report path]`.
- Deterministic (temp 0, cached), reproducible; results written to `data/eval/` over time.
- CI gate compares against a stored baseline and fails on regression beyond a threshold.
- Every pipeline upgrade is behind a flag; the harness A/Bs it before it goes live.

## Cross-cutting

Multilingual FTS, chunk granularity (ties to citations Phase-3 ingestion), free-tier
rate limits, cache correctness under rewriting, latency budget, determinism, online +
offline signals, feature-flagged rollout with the current path as fallback.

## Open decisions

1. Golden data: which real BRDs, how many cases, verification (LLM-draft + human spot-check).
2. Expansion appetite: local + condense-folded only (zero-cost) vs also embed-multi-query (costs).
3. Answer judge: reuse `refine.py` vs dedicated rubric judge.
4. CI gate thresholds + subset vs nightly.
5. Budget: strictly free-tier (assumed here) vs credit.
