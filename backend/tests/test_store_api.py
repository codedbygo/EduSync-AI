"""Durable records: survive a restart, recover interrupted jobs, teacher overrides, CSV export, delete."""
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import grading, main, store

S = Path(__file__).resolve().parents[2] / "samples"
RUB = {"title": "Photosynthesis", "questions": [{"id": "Q1", "text": "Explain.", "criteria": [
    {"id": "c1", "description": "Defines it", "marks": 2}, {"id": "c2", "description": "Equation", "marks": 3}]}]}
AI = {"total_score": 4.0, "max_total": 5.0, "needs_review": True, "ocr_confidence": 0.9, "second_opinion": True, "transcript": "t",
      "questions": [{"question_id": "Q1", "question": "Explain.", "score": 4.0, "max_marks": 5.0, "missing_concepts": [], "incorrect_statements": [],
                     "criteria": [{"criterion_id": "c1", "description": "Defines it", "max_marks": 2, "marks_awarded": 2, "reasoning": "", "evidence_quote": "=SUM(A1)",
                                   "evidence_verified": True, "confidence": 0.9, "needs_review": False, "flags": []},
                                  {"criterion_id": "c2", "description": "Equation", "max_marks": 3, "marks_awarded": 2, "reasoning": "", "evidence_quote": "",
                                   "evidence_verified": False, "confidence": 0.4, "needs_review": True, "flags": ["x"]}]}]}


@pytest.fixture(params=["local", "db"])  # files in a folder, or inside the database (the Neon setup)
def env(tmp_path, monkeypatch, request):
    for k in ("DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BLOB_STORE", request.param)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setattr(main, "DATA", tmp_path)
    monkeypatch.setattr(grading, "evaluate", lambda rubric, pages, progress=None: dict(AI))
    main.jobs.clear()
    yield tmp_path
    main.jobs.clear()


def wait(c, url, stages=("done", "error")):
    for _ in range(200):
        s = c.get(url).json()
        if s["stage"] in stages:
            return s
        time.sleep(0.05)
    raise AssertionError("timeout")


def submit(c, student="Asha R"):
    with open(S / "answer_correct.png", "rb") as f:
        r = c.post("/api/eval/jobs", data={"rubric": __import__("json").dumps(RUB), "student": student},
                   files=[("pages", ("p1.png", f, "image/png"))])
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_evaluation_is_a_durable_record_with_teacher_review(env):
    with TestClient(main.app) as c:
        jid = submit(c)
        s = wait(c, f"/api/eval/jobs/{jid}")
        assert s["stage"] == "done" and s["student"] == "Asha R" and s["title"] == "Photosynthesis"
        assert s["result"]["final_total"] == 4.0 and s["result"]["open_reviews"] == 1

        # teacher review: bounded by the rubric, AI mark kept beside it
        assert c.put(f"/api/eval/jobs/{jid}/overrides/Q1/c2", json={"marks": 3.5}).status_code == 422
        assert c.put(f"/api/eval/jobs/{jid}/overrides/Q1/nope", json={"marks": 1}).status_code == 404
        r = c.put(f"/api/eval/jobs/{jid}/overrides/Q1/c2", json={"marks": 3, "note": "equation is right, OCR misread"}).json()
        c2 = r["result"]["questions"][0]["criteria"][1]
        assert c2["marks_awarded"] == 2 and c2["teacher_marks"] == 3 and c2["final_marks"] == 3
        assert r["result"]["final_total"] == 5.0 and r["result"]["total_score"] == 4.0 and r["result"]["open_reviews"] == 0

        # CSV: spreadsheet formulas from student text are neutralised
        csv = c.get(f"/api/eval/jobs/{jid}/export.csv").text
        assert "'=SUM(A1)" in csv and "equation is right" in csv
        assert "Asha R" in c.get("/api/eval/export.csv").text

    # "restart": memory and working files gone, only the store remains
    main.jobs.clear()
    shutil.rmtree(env / jid)
    with TestClient(main.app) as c:
        s = c.get(f"/api/eval/jobs/{jid}").json()
        assert s["result"]["final_total"] == 5.0 and s["overrides"]["Q1"]["c2"]["note"].startswith("equation")
        assert c.get(f"/api/eval/jobs/{jid}/pages/1").status_code == 200  # page image served from the blob store
        lst = c.get("/api/eval/jobs").json()
        assert lst["total"] == 1 and lst["items"][0]["final_total"] == 5.0 and lst["items"][0]["student"] == "Asha R"
        assert c.delete(f"/api/eval/jobs/{jid}").status_code == 204
        assert c.get(f"/api/eval/jobs/{jid}").status_code == 404
        assert c.get("/api/eval/jobs").json()["total"] == 0


def test_job_killed_by_a_restart_is_marked_interrupted_and_can_be_retried(env):
    with TestClient(main.app) as c:
        jid = submit(c)
        wait(c, f"/api/eval/jobs/{jid}")
    rec = store.get(jid)
    rec.update(stage="running", result=None)  # as if the process died mid-evaluation
    store.save(rec, main.summarize(rec))
    main.jobs.clear()
    shutil.rmtree(env / jid)
    with TestClient(main.app) as c:
        s = c.get(f"/api/eval/jobs/{jid}").json()
        assert s["stage"] == "interrupted" and "Retry" in s["error"]
        assert c.post(f"/api/eval/jobs/{jid}/retry").status_code == 200
        assert wait(c, f"/api/eval/jobs/{jid}")["stage"] == "done"  # pages came back from the blob store
        assert c.post(f"/api/eval/jobs/{jid}/retry").status_code == 409  # a finished job is not retried


def test_running_job_cannot_be_deleted_and_bad_input_is_rejected(env, monkeypatch):
    monkeypatch.setattr(grading, "evaluate", lambda rubric, pages, progress=None: (time.sleep(1), dict(AI))[1])
    with TestClient(main.app) as c:
        jid = submit(c)
        assert c.delete(f"/api/eval/jobs/{jid}").status_code == 409
        assert c.post("/api/eval/jobs", data={"rubric": "{"}, files=[("pages", ("a.png", b"x", "image/png"))]).status_code == 422
        assert c.get("/api/eval/jobs?limit=0").status_code == 422
        assert c.post("/api/video/jobs/doesnotexist/render", json={"timeline": [{"slide": 1, "start": 0}]}).status_code == 404
        assert c.get("/api/health").json()["database"] == "sqlite"


def test_database_file_store_round_trip_and_size_cap(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("BLOB_STORE", "db")
    monkeypatch.setenv("BLOB_MAX_MB", "1")
    store.init(tmp_path)
    small, big = tmp_path / "a.png", tmp_path / "b.mp4"
    small.write_bytes(b"\x89PNG" * 100)
    big.write_bytes(b"0" * (2 << 20))
    store.blobs.put("eval/x/a.png", small, "image/png")
    store.blobs.put("eval/x/a.png", small, "image/png")  # upsert, not a duplicate-key error
    assert store.blobs.fetch("eval/x/a.png", tmp_path / "out.png") and (tmp_path / "out.png").read_bytes() == small.read_bytes()
    with pytest.raises(store.StorageError, match="per-file limit"):
        store.blobs.put("video/x/result.mp4", big, "video/mp4")
    store.blobs.delete(["eval/x/a.png"])
    assert not store.blobs.fetch("eval/x/a.png", tmp_path / "gone.png")


def test_supabase_storage_requests(monkeypatch, tmp_path):
    import httpx
    calls = []

    class R:
        def __init__(self, code, body=None, text=""):
            self.status_code, self._b, self.text = code, body, text

        def json(self):
            return self._b

    def post(url, **kw):
        calls.append((url, kw.get("headers", {}), kw.get("json")))
        if url.endswith("/bucket"):
            return R(400, text='{"message":"The resource already exists"}')
        if "/object/sign/" in url:
            return R(200, {"signedURL": "/object/sign/edusync/video/x/result.mp4?token=t"})
        return R(200)
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setenv("SUPABASE_URL", "https://p.supabase.co/")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")
    b = store._Blobs()
    b.init(tmp_path)  # existing bucket is fine
    f = tmp_path / "r.mp4"
    f.write_bytes(b"x")
    b.put("video/x/result.mp4", f, "video/mp4")
    url, h, _ = calls[-1]
    assert url == "https://p.supabase.co/storage/v1/object/edusync/video/x/result.mp4" and h["x-upsert"] == "true" and h["Authorization"] == "Bearer k"
    assert b.signed_url("video/x/result.mp4", "a.mp4") == "https://p.supabase.co/storage/v1/object/sign/edusync/video/x/result.mp4?token=t&download=a.mp4"


def test_postgres_url_uses_psycopg3(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@aws-0-ap-south-1.pooler.supabase.com:5432/postgres")
    assert store._db_url(tmp_path).startswith("postgresql+psycopg://")
    monkeypatch.delenv("DATABASE_URL")
    assert store._db_url(tmp_path).startswith("sqlite:///")
