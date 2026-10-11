"""Object storage for uploaded BRD source files.

Two interchangeable backends behind one tiny interface (put / get / delete / exists):
  * S3Store    — any S3-compatible service (MinIO locally via docker compose, AWS S3,
                 Cloudflare R2, …). Selected with STORAGE_BACKEND=s3 + S3_* in .env.
  * LocalStore — a directory on disk. Dev / tests only (STORAGE_BACKEND=local).

Keys are content-addressed per owner — `brd-sources/<owner>/<sha256><ext>` — so the same
file uploaded twice is stored once, and a source can always be re-processed (e.g. after a
parser improvement) without asking the user to upload it again.

Deletes: objects are removed only when the BRD that owns them is deleted, AFTER the DB
transaction that removed the rows has committed (so a failed DB delete never leaves rows
pointing at a missing object). `gc_orphans` sweeps any object no row references.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.core.config import settings

log = logging.getLogger("brd.storage")


class StorageError(RuntimeError):
    """The object store is unreachable or refused an operation."""


def source_key(owner_id: int, sha256: str, filename: str) -> str:
    ext = Path(filename or "").suffix.lower()[:10]
    return f"brd-sources/{int(owner_id)}/{sha256}{ext}"


class LocalStore:
    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:                  # never escape the root
            raise StorageError(f"invalid key {key!r}")
        return p

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, p)                              # atomic publish

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as e:
            raise StorageError(f"object {key!r} not found") from e

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def keys(self, prefix: str = "") -> list[str]:
        base = self.root
        return [str(p.relative_to(base)).replace("\\", "/") for p in base.rglob("*")
                if p.is_file() and str(p.relative_to(base)).replace("\\", "/").startswith(prefix)]

    def list_objects(self, prefix: str = "") -> list[tuple[str, datetime]]:
        """Keys under `prefix` with their last-modified time (mtime) — lets a GC sweep skip
        objects too young to safely judge as orphaned (a submit's put may race its DB insert)."""
        base = self.root
        out: list[tuple[str, datetime]] = []
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(base)).replace("\\", "/")
            if rel.startswith(prefix):
                out.append((rel, datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)))
        return out


class S3Store:
    def __init__(self):
        import boto3
        from botocore.config import Config

        self.bucket = settings.storage_bucket
        self.s3 = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.need(settings.s3_access_key, "S3_ACCESS_KEY"),
            aws_secret_access_key=settings.need(settings.s3_secret_key, "S3_SECRET_KEY"),
            config=Config(retries={"max_attempts": 4, "mode": "standard"},
                          connect_timeout=5, read_timeout=60, s3={"addressing_style": "path"}),
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        from botocore.exceptions import ClientError
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except ClientError:
            try:
                self.s3.create_bucket(Bucket=self.bucket)
                log.info("storage: created bucket %s", self.bucket)
            except ClientError as e:
                raise StorageError(f"cannot create bucket {self.bucket!r}: {e}") from e
        except Exception as e:  # noqa: BLE001 — endpoint down / bad creds
            raise StorageError(f"object store unreachable: {e}") from e

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=data,
                               ContentType=content_type or "application/octet-stream")
        except Exception as e:  # noqa: BLE001
            raise StorageError(f"upload of {key!r} failed: {e}") from e

    def get(self, key: str) -> bytes:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as e:  # noqa: BLE001
            raise StorageError(f"download of {key!r} failed: {e}") from e

    def exists(self, key: str) -> bool:
        """False only for a genuine 404/NoSuchKey; any other ClientError (bad credentials,
        permission denied, endpoint down, …) is a real failure and must not be swallowed as
        "doesn't exist" — a caller using this for a health check would otherwise report
        healthy against a store it can't actually reach."""
        from botocore.exceptions import ClientError
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in ("404", "NoSuchKey") or status == 404:
                return False
            raise StorageError(f"exists check for {key!r} failed: {e}") from e

    def delete(self, key: str) -> None:
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        except Exception as e:  # noqa: BLE001
            raise StorageError(f"delete of {key!r} failed: {e}") from e

    def keys(self, prefix: str = "") -> list[str]:
        out: list[str] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            out.extend(o["Key"] for o in page.get("Contents", []))
        return out

    def list_objects(self, prefix: str = "") -> list[tuple[str, datetime]]:
        """Keys under `prefix` with their LastModified time — lets a GC sweep skip objects
        too young to safely judge as orphaned (a submit's put may race its DB insert)."""
        out: list[tuple[str, datetime]] = []
        try:
            for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
                for o in page.get("Contents", []):
                    out.append((o["Key"], o["LastModified"]))
        except Exception as e:  # noqa: BLE001 — botocore errors → the module's one storage error type
            raise StorageError(f"listing {prefix!r} failed: {e}") from e
        return out


_store = None
_lock = threading.Lock()


def store():
    """The configured object store (created lazily, once)."""
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                _store = S3Store() if settings.storage_backend == "s3" else LocalStore(settings.storage_local_dir)
    return _store


def delete_quietly(keys: list[str]) -> int:
    """Best-effort delete after a committed DB delete; failures are logged (gc_orphans
    catches them later). Returns how many were removed."""
    n = 0
    for k in keys:
        try:
            store().delete(k)
            n += 1
        except Exception:  # noqa: BLE001
            log.warning("storage: could not delete %s (left for gc)", k, exc_info=True)
    return n


def gc_orphans(referenced: set[str], prefix: str = "brd-sources/", min_age_seconds: int = 0) -> int:
    """Delete stored objects that no DB row references any more. `min_age_seconds` (default 0,
    unchanged behavior) skips objects younger than that, so a sweep never races a concurrent
    submit/upload that has put the object but not yet committed the DB row referencing it."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=min_age_seconds) if min_age_seconds else None
    stale = [k for k, modified in store().list_objects(prefix)
             if k not in referenced and (cutoff is None or modified <= cutoff)]
    return delete_quietly(stale)
