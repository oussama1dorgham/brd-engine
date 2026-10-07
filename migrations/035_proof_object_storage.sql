-- 035 — Test evidence files move to object storage (same store as BRD sources: MinIO / S3 / local).
--
-- use_case_test_proof.object_key = the object holding the file (`use-case-proofs/<owner>/<random>`).
-- `content` (bytea) stays as the fallback when the store is unreachable at upload time, and for rows
-- not yet moved; the app moves existing blobs in the background (testing.move_blobs_to_storage) and
-- sweeps objects no row references (testing.gc_proof_objects). Objects are deleted only AFTER the
-- DB delete commits, so a row never points at a missing object.
--
-- brd_real only (FK to app_user). Apply:  python -m backend.migrate

ALTER TABLE use_case_test_proof ADD COLUMN IF NOT EXISTS object_key text;
CREATE UNIQUE INDEX IF NOT EXISTS use_case_test_proof_object_uk ON use_case_test_proof(object_key)
    WHERE object_key IS NOT NULL;
