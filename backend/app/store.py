"""Durable storage: one table of job records and a blob store for files.

DATABASE_URL set (e.g. Supabase Postgres, session pooler URL) -> Postgres; unset -> SQLite under DATA_DIR.
SUPABASE_URL + SUPABASE_SERVICE_KEY set -> files in Supabase Storage; unset -> files under DATA_DIR/blobs.
The server disk is only a working cache: anything a user must get back later is written here.
"""
import json
import os
import shutil
import time
from pathlib import Path

import httpx
from sqlalchemy import Column, Float, MetaData, String, Table, Text, create_engine, delete, func, insert, select, update

TERMINAL = {"done", "review", "error", "interrupted"}  # a job in any other stage is running

meta = MetaData()
records = Table(
    "edusync_jobs", meta,
    Column("id", String(32), primary_key=True),
    Column("kind", String(16), nullable=False, index=True),
    Column("title", String(300), nullable=False, default=""),
    Column("stage", String(32), nullable=False),
    Column("created_at", Float, nullable=False, index=True),
    Column("updated_at", Float, nullable=False),
    Column("summary", Text, nullable=False, default="{}"),  # small JSON for history lists
    Column("data", Text, nullable=False),                   # the full job record as JSON
)

_engine = None


def _db_url(data_dir: Path) -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{data_dir / 'edusync.db'}"
    for old in ("postgres://", "postgresql://"):  # Supabase/Render give the bare scheme; use the psycopg 3 driver
        if url.startswith(old):
            return "postgresql+psycopg://" + url[len(old):]
    return url


def init(data_dir: Path) -> None:
    global _engine
    data_dir.mkdir(parents=True, exist_ok=True)
    url = _db_url(data_dir)
    kw: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False}  # worker threads share the engine
    _engine = create_engine(url, **kw)
    meta.create_all(_engine)  # ponytail: create-if-missing, no migration tool; add Alembic when the schema first changes
    blobs.init(data_dir)


def _eng():
    if _engine is None:
        raise RuntimeError("store.init() was not called")
    return _engine


def save(job: dict, summary: dict) -> None:
    now = time.time()
    row = {"kind": job["kind"], "title": str(job.get("title", ""))[:300], "stage": job["stage"],
           "updated_at": now, "summary": json.dumps(summary), "data": json.dumps(job)}
    with _eng().begin() as c:
        if c.execute(update(records).where(records.c.id == job["id"]).values(**row)).rowcount == 0:
            c.execute(insert(records).values(id=job["id"], created_at=job.get("created", now), **row))


def get(jid: str) -> dict | None:
    with _eng().connect() as c:
        r = c.execute(select(records.c.data).where(records.c.id == jid)).first()
    return json.loads(r[0]) if r else None


def list_jobs(kind: str, limit: int, offset: int) -> tuple[list[dict], int]:
    with _eng().connect() as c:
        total = c.execute(select(func.count()).select_from(records).where(records.c.kind == kind)).scalar_one()
        rows = c.execute(
            select(records.c.id, records.c.title, records.c.stage, records.c.created_at, records.c.updated_at, records.c.summary)
            .where(records.c.kind == kind).order_by(records.c.created_at.desc()).limit(limit).offset(offset)
        ).all()
    return [{"id": r.id, "title": r.title, "stage": r.stage, "created_at": r.created_at, "updated_at": r.updated_at,
             **json.loads(r.summary)} for r in rows], int(total)


def remove(jid: str) -> None:
    with _eng().begin() as c:
        c.execute(delete(records).where(records.c.id == jid))


def running_ids() -> list[str]:
    with _eng().connect() as c:
        return [r[0] for r in c.execute(select(records.c.id).where(records.c.stage.not_in(TERMINAL))).all()]


# ---------------- blobs ----------------
class _Blobs:
    """Local folder or Supabase Storage (REST). Keys look like 'video/<id>/result.mp4'."""

    def init(self, data_dir: Path) -> None:
        self.url = os.getenv("SUPABASE_URL", "").rstrip("/")
        self.key = os.getenv("SUPABASE_SERVICE_KEY", "")
        self.bucket = os.getenv("SUPABASE_BUCKET", "edusync")
        self.local = data_dir / "blobs"
        if self.remote:
            r = httpx.post(f"{self.url}/storage/v1/bucket", headers=self._h(), timeout=30,
                           json={"id": self.bucket, "name": self.bucket, "public": False})
            if r.status_code not in (200, 201, 400, 409) or (r.status_code == 400 and "exist" not in r.text.lower()):
                raise RuntimeError(f"Supabase Storage: cannot create bucket '{self.bucket}': HTTP {r.status_code} {r.text[:200]}")
        else:
            self.local.mkdir(parents=True, exist_ok=True)

    @property
    def remote(self) -> bool:
        return bool(self.url and self.key)

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.key}", "apikey": self.key}

    def put(self, key: str, src: Path, content_type: str = "application/octet-stream") -> None:
        if not self.remote:
            dst = self.local / key
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            return
        with open(src, "rb") as f:
            r = httpx.post(f"{self.url}/storage/v1/object/{self.bucket}/{key}", content=f, timeout=600,
                           headers={**self._h(), "Content-Type": content_type, "x-upsert": "true"})
        if r.status_code not in (200, 201):
            raise StorageError(f"Could not store {key.rsplit('/', 1)[-1]} ({src.stat().st_size >> 20} MB): HTTP {r.status_code} {r.text[:160]}")

    def fetch(self, key: str, dst: Path) -> bool:
        """Copy the blob to dst. False if it does not exist."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not self.remote:
            p = self.local / key
            if not p.exists():
                return False
            shutil.copyfile(p, dst)
            return True
        with httpx.stream("GET", f"{self.url}/storage/v1/object/{self.bucket}/{key}", headers=self._h(), timeout=600) as r:
            if r.status_code != 200:
                return False
            with open(dst, "wb") as out:
                for chunk in r.iter_bytes(1 << 20):
                    out.write(chunk)
        return True

    def local_path(self, key: str) -> Path | None:
        p = self.local / key
        return p if not self.remote and p.exists() else None

    def signed_url(self, key: str, download_name: str | None = None) -> str | None:
        if not self.remote:
            return None
        r = httpx.post(f"{self.url}/storage/v1/object/sign/{self.bucket}/{key}", headers=self._h(), timeout=30, json={"expiresIn": 3600})
        if r.status_code != 200:
            return None
        u = f"{self.url}/storage/v1{r.json()['signedURL']}"
        return u + (f"&download={download_name}" if download_name else "")

    def delete(self, keys: list[str]) -> None:
        if not keys:
            return
        if not self.remote:
            for k in keys:
                (self.local / k).unlink(missing_ok=True)
            return
        httpx.request("DELETE", f"{self.url}/storage/v1/object/{self.bucket}", headers=self._h(), json={"prefixes": keys}, timeout=60)


class StorageError(RuntimeError):
    pass


blobs = _Blobs()
