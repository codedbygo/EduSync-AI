"""EduSync AI API. Two features, durable job records (see store.py), a static frontend."""
import csv
import io
import json
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import grading, llm, store, video

_env = Path(__file__).resolve().parents[2] / ".env"  # local dev: KEY=value lines, real env vars win
if _env.exists() and not os.getenv("EDUSYNC_NO_DOTENV"):  # tests set this so a developer .env cannot change them
    for _l in _env.read_text().splitlines():
        if "=" in _l and not _l.lstrip().startswith("#"):
            _k, _v = _l.split("=", 1)
            if _v.strip():
                os.environ.setdefault(_k.strip(), _v.strip())

DATA = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parents[2] / "data"))  # SQLite + local files when Supabase is not set
MAX_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
CACHE_TTL = 6 * 3600
STATIC = Path(os.getenv("STATIC_DIR", Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"))
INSTANCE = os.getenv("INSTANCE_NAME") or __import__("socket").gethostname()  # which server runs a job (laptop and Render can share one database)
INTERRUPTED = "The server restarted while this job was running, so it stopped. Press Retry to run it again from the stored inputs."


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init(DATA)
    for jid in store.running_ids():  # a restart killed this server's threads: say so instead of showing "running" forever
        rec = store.get(jid)
        if rec and rec.get("owner", INSTANCE) == INSTANCE:  # never touch a job another server is running right now
            rec.update(stage="interrupted", error=INTERRUPTED, message="Interrupted")
            store.save(rec, summarize(rec))
    yield


app = FastAPI(title="EduSync AI", lifespan=lifespan)
heavy = ThreadPoolExecutor(max_workers=1)  # whisper + ffmpeg are CPU bound: one at a time
light = ThreadPoolExecutor(max_workers=4)  # LLM calls are network bound
jobs: dict[str, dict] = {}  # live cache of jobs touched by this process; the store is the record
lock = threading.Lock()
plock = threading.Lock()  # serialises snapshot+write so an older snapshot never overwrites a newer one
_last_save: dict[str, float] = {}

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
DECK_EXT = {".pptx", ".ppt", ".pdf", ".odp"}
IMG_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
RUNNING = {"queued", "slides", "transcribing", "matching", "rendering", "running", "saving"}


# ---------------- job records ----------------
def summarize(j: dict) -> dict:
    if j["kind"] == "eval":
        res = j.get("result")
        final = grading.apply_overrides(res, j.get("overrides", {})) if res else None
        return {"student": j.get("student", ""), "pages": len(j.get("files", {}).get("pages", [])), "error": j.get("error"),
                "ai_total": res and res["total_score"], "final_total": final and final["final_total"],
                "max_total": res and res["max_total"], "needs_review": bool(res and res["needs_review"]),
                "open_reviews": final["open_reviews"] if final else None}
    r = j.get("result") or {}
    return {"video_name": j.get("video_name", ""), "deck_name": j.get("deck_name", ""), "n_slides": j.get("n_slides"),
            "duration": r.get("duration"), "has_result": bool(r), "weak": j.get("weak"), "error": j.get("error")}


def persist(jid: str) -> None:
    with plock:
        with lock:
            snap = json.loads(json.dumps(jobs[jid]))
        store.save(snap, summarize(snap))
        _last_save[jid] = time.time()


def new_job(kind: str, **fields) -> tuple[str, Path]:
    now = time.time()
    with lock:  # drop old finished jobs from the cache and their working files; their records stay in the store
        for jid in [j for j, v in jobs.items() if now - v["created"] > CACHE_TTL and v["stage"] not in RUNNING]:
            shutil.rmtree(DATA / jid, ignore_errors=True)
            jobs.pop(jid)
        jid = uuid.uuid4().hex[:12]
        jobs[jid] = {"id": jid, "kind": kind, "stage": "queued", "progress": 0.0, "message": "Queued", "error": None,
                     "created": now, "files": {}, "owner": INSTANCE, **fields}
    d = DATA / jid
    d.mkdir(parents=True, exist_ok=True)
    return jid, d


def update(jid: str, **kw) -> None:
    with lock:
        jobs[jid].update(kw)
    # progress ticks are frequent: write those at most every 2 s; any other change is written at once
    if set(kw) - {"progress", "message"} or time.time() - _last_save.get(jid, 0) > 2:
        persist(jid)


def load(jid: str, kind: str) -> dict:
    """The job, live from the cache if this process runs it, else from the store (then cached)."""
    with lock:
        j = jobs.get(jid)
    if j is None:
        j = store.get(jid)
        if j is not None:
            with lock:
                j = jobs.setdefault(jid, j)
    if not j or j["kind"] != kind:
        raise HTTPException(404, "Job not found.")
    return j


def runner(jid: str, fn, *args):
    def go():
        try:
            fn(jid, *args)
        except (video.VideoError, llm.LLMError, grading.RubricError, store.StorageError) as e:
            update(jid, stage="error", error=str(e), message="Failed")
        except Exception as e:  # last resort: surface it, never hang the UI on "running"
            update(jid, stage="error", error=f"Unexpected error: {type(e).__name__}: {e}", message="Failed")
    return go


def keep(jid: str, key: str, path: Path, ctype: str) -> str | None:
    """Store a file durably. A storage failure is recorded as a warning, never fails the job: the local copy still serves."""
    try:
        store.blobs.put(key, path, ctype)
        return key
    except store.StorageError as e:
        update(jid, storage_warning=f"{e} of the free cloud storage, so it is kept on the server's own disk instead. It still plays and downloads normally.")
        return None


def serve(local: Path, key: str | None, media_type: str, download: str | None = None):
    if local.exists():
        return FileResponse(local, media_type=media_type, filename=download)
    if key:
        if (p := store.blobs.local_path(key)) is not None:
            return FileResponse(p, media_type=media_type, filename=download)
        if (u := store.blobs.signed_url(key, download)) is not None:
            return RedirectResponse(u, status_code=307)
        if store.blobs.fetch(key, local):  # database store: bring it back into the working cache, then serve with range support
            return FileResponse(local, media_type=media_type, filename=download)
    raise HTTPException(404, "File not available.")


def delete_job(jid: str, kind: str) -> Response:
    j = load(jid, kind)
    if j["stage"] in RUNNING:
        raise HTTPException(409, "This job is still running. Wait for it to finish, then delete it.")
    f = j.get("files", {})
    keys = [k for v in f.values() for k in (v if isinstance(v, list) else [v]) if k]
    store.blobs.delete(keys)
    store.remove(jid)
    with lock:
        jobs.pop(jid, None)
    shutil.rmtree(DATA / jid, ignore_errors=True)
    return Response(status_code=204)


def save_upload(f: UploadFile, dest: Path, allowed: set[str]) -> Path:
    ext = Path(f.filename or "").suffix.lower()
    if ext not in allowed:
        raise HTTPException(415, f"'{f.filename}': unsupported type. Allowed: {', '.join(sorted(allowed))}")
    path = dest.with_suffix(ext)
    size = 0
    with open(path, "wb") as out:
        while chunk := f.file.read(1 << 20):
            size += len(chunk)
            if size > MAX_MB << 20:
                out.close()
                path.unlink(missing_ok=True)
                raise HTTPException(413, f"'{f.filename}' is larger than {MAX_MB} MB.")
            out.write(chunk)
    if size == 0:
        raise HTTPException(400, f"'{f.filename}' is empty.")
    return path


def page_list(limit: int, offset: int, kind: str) -> dict:
    items, total = store.list_jobs(kind, limit, offset)
    with lock:  # live progress for jobs this process is running
        for it in items:
            if it["id"] in jobs:
                it["stage"] = jobs[it["id"]]["stage"]
    return {"items": items, "total": total}


@app.get("/api/health")
def health():
    return {"ok": True, "llm_configured": llm.available(), "model": llm._model(), "provider": llm.default_provider(),
            "storage": {"supabase": "supabase", "db": "database", "local": "local"}[store.blobs.mode],
            "database": "postgres" if store._eng().dialect.name == "postgresql" else "sqlite"}


# ---------------- Feature 1: video + slides ----------------
def ensure_local(jid: str) -> Path:
    """Working files for a job, fetched back from the store after a restart or cache cleanup."""
    d = DATA / jid
    j = jobs[jid]
    f = j["files"]
    if not list(d.glob("video.*")) and f.get("video"):
        if not store.blobs.fetch(f["video"], d / Path(f["video"]).name):
            raise video.VideoError("The stored source video is missing. Upload it again.")
    if not list(d.glob("deck.*")) and f.get("deck"):
        store.blobs.fetch(f["deck"], d / Path(f["deck"]).name)
    for k in f.get("slides", []):
        p = d / "slides" / Path(k).name
        if not p.exists():
            store.blobs.fetch(k, p)
    return d


def analyse(jid: str, auto_render: bool):
    d = ensure_local(jid)
    vpath = next(d.glob("video.*"))
    deck = next(d.glob("deck.*"))
    update(jid, stage="saving", progress=0.02, message="Saving uploads")
    files = dict(jobs[jid]["files"])
    files["video"] = files.get("video") or keep(jid, f"video/{jid}/{vpath.name}", vpath, "video/mp4")
    files["deck"] = files.get("deck") or keep(jid, f"video/{jid}/{deck.name}", deck, "application/octet-stream")
    update(jid, files=files, stage="slides", progress=0.05,
           message="Converting slides to images" + (" (LibreOffice; a PDF upload skips this step)" if deck.suffix.lower() != ".pdf" else ""))
    imgs, texts = video.slides_to_images(deck, d / "slides", lambda f: update(jid, progress=0.05 + 0.06 * f, message=f"Drawing slides ({round(f * 100)}%)"))
    update(jid, progress=0.11, message=f"Saving {len(imgs)} slide images")
    with ThreadPoolExecutor(8) as ex:  # network bound: parallel uploads instead of one at a time
        keys = list(ex.map(lambda p: keep(jid, f"video/{jid}/slides/{p.name}", p, "image/png"), imgs))
    files["slides"] = [k for k in keys if k]
    update(jid, files=files, n_slides=len(imgs), slide_texts=texts, stage="transcribing", progress=0.15, message="Transcribing speech (Whisper)")
    duration = video.probe_duration(vpath)
    segs = video.transcribe(vpath)
    update(jid, segments=segs, duration=duration, stage="matching", progress=0.6, message="Matching slides to speech")
    m = video.match(texts, segs, duration)
    update(jid, timeline=m["timeline"], weak=m["weak"], note=m["note"])
    if auto_render and not m["weak"]:
        render_job(jid, m["timeline"], "auto")
    else:
        update(jid, stage="review", progress=0.7, message="Review the timeline, then render")


def render_job(jid: str, timeline: list[dict], chroma: str):
    d = ensure_local(jid)
    update(jid, stage="rendering", progress=0.0, message="Rendering video", result=None, pending_render={"timeline": timeline, "chroma": chroma})
    info = video.render(next(d.glob("video.*")), sorted((d / "slides").glob("slide_*.png")), timeline, d / "result.mp4", d, chroma=chroma,
                        progress=lambda p, m: update(jid, progress=0.7 + 0.3 * p))
    update(jid, progress=0.99, message="Saving the video")
    files = dict(jobs[jid]["files"])
    files["result"] = keep(jid, f"video/{jid}/result.mp4", d / "result.mp4", "video/mp4")
    update(jid, files=files, stage="done", progress=1.0, message="Done", result=info, timeline=timeline, pending_render=None)


@app.post("/api/video/jobs")
def create_video_job(video_file: UploadFile = File(...), deck_file: UploadFile = File(...), auto_render: bool = Form(True)):
    jid, d = new_job("video", title=Path(video_file.filename or "video").stem[:200], video_name=video_file.filename or "",
                     deck_name=deck_file.filename or "", auto_render=auto_render)
    try:
        save_upload(video_file, d / "video", VIDEO_EXT)
        save_upload(deck_file, d / "deck", DECK_EXT)
    except HTTPException:
        shutil.rmtree(d, ignore_errors=True)
        with lock:
            jobs.pop(jid, None)
        raise
    persist(jid)
    heavy.submit(runner(jid, analyse, auto_render))
    return {"id": jid}


class TimelineItem(BaseModel):
    slide: int = Field(ge=1)
    start: float = Field(ge=0)


class RenderBody(BaseModel):
    timeline: list[TimelineItem] = Field(min_length=1, max_length=2000)
    chroma: Literal["auto", "on", "off"] = "auto"


@app.post("/api/video/jobs/{jid}/render")
def rerender(jid: str, body: RenderBody):
    job = load(jid, "video")
    if job["stage"] in RUNNING:
        raise HTTPException(409, "Job is still busy.")
    if not job.get("files", {}).get("slides") and not (DATA / jid / "slides").exists():
        raise HTTPException(409, "This job has no slides yet. Press Retry first.")
    update(jid, stage="rendering", error=None, message="Rendering video", progress=0.0, owner=INSTANCE)
    heavy.submit(runner(jid, render_job, [t.model_dump() for t in body.timeline], body.chroma))
    return {"id": jid}


@app.post("/api/video/jobs/{jid}/retry")
def retry_video(jid: str):
    job = load(jid, "video")
    if job["stage"] not in ("error", "interrupted"):
        raise HTTPException(409, "Only a failed or interrupted job can be retried.")
    pr = job.get("pending_render")
    update(jid, stage="queued", error=None, message="Queued", progress=0.0, owner=INSTANCE)
    if pr:  # it died while rendering: render the same timeline again
        heavy.submit(runner(jid, render_job, pr["timeline"], pr["chroma"]))
    else:
        heavy.submit(runner(jid, analyse, job.get("auto_render", True)))
    return {"id": jid}


@app.get("/api/video/jobs")
def list_video_jobs(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    return page_list(limit, offset, "video")


@app.get("/api/video/jobs/{jid}")
def video_status(jid: str):
    job = dict(load(jid, "video"))
    n = job.get("n_slides") or 0
    job["slide_urls"] = [f"/api/video/jobs/{jid}/slides/{i}" for i in range(1, n + 1)]
    job.pop("slide_texts", None)
    return job


@app.delete("/api/video/jobs/{jid}", status_code=204)
def delete_video(jid: str):
    return delete_job(jid, "video")


@app.get("/api/video/jobs/{jid}/slides/{n}")
def slide_image(jid: str, n: int):
    j = load(jid, "video")
    name = f"slide_{n:03d}.png"
    key = next((k for k in j.get("files", {}).get("slides", []) if k.endswith("/" + name)), None)
    return serve(DATA / jid / "slides" / name, key, "image/png")


@app.get("/api/video/jobs/{jid}/source")
def source_video(jid: str):
    j = load(jid, "video")
    local = next((DATA / jid).glob("video.*"), DATA / jid / "video.missing")
    return serve(local, j.get("files", {}).get("video"), "video/mp4")


@app.get("/api/video/jobs/{jid}/result.mp4")
def result_video(jid: str, download: bool = False):
    j = load(jid, "video")
    if not j.get("result"):
        raise HTTPException(404, "Video not rendered yet.")
    return serve(DATA / jid / "result.mp4", j.get("files", {}).get("result"), "video/mp4",
                 f"{j.get('title') or 'edusync'}_synced.mp4" if download else None)


# ---------------- Feature 2: handwritten evaluation ----------------
def evaluate_job(jid: str):
    j = jobs[jid]
    d = DATA / jid
    pages = []
    for i, key in enumerate(j["files"]["pages"]):
        p = d / Path(key).name
        if not p.exists() and not store.blobs.fetch(key, p):
            raise grading.RubricError(f"Stored page {i + 1} is missing; submit the evaluation again.")
        pages.append((p.read_bytes(), IMG_EXT[p.suffix.lower()]))
    update(jid, stage="running", progress=0.05, message="Starting")
    res = grading.evaluate(j["rubric"], pages, lambda p, m: update(jid, progress=p, message=m))
    update(jid, stage="done", progress=1.0, message="Done", result=res)


@app.post("/api/eval/jobs")
def create_eval_job(rubric: str = Form(...), pages: list[UploadFile] = File(...),
                    student: str = Form("", max_length=200), title: str = Form("", max_length=200)):
    try:
        rub = json.loads(rubric)
        grading.validate_rubric(rub)
    except json.JSONDecodeError as e:
        raise HTTPException(422, f"Rubric is not valid JSON: {e}")
    except grading.RubricError as e:
        raise HTTPException(422, str(e))
    if not llm.available():
        raise HTTPException(503, "No model is configured on the server (GEMINI_API_KEY, OPENROUTER_API_KEY or Ollama), so handwriting cannot be read.")
    if not pages or len(pages) > 12:
        raise HTTPException(413, "Upload between 1 and 12 pages per submission.")
    for f in pages:
        if Path(f.filename or "").suffix.lower() not in IMG_EXT:
            raise HTTPException(415, f"'{f.filename}': upload PNG, JPG or WebP images.")
    jid, d = new_job("eval", rubric=rub, student=student.strip(), overrides={},
                     title=title.strip() or str(rub.get("title") or "Evaluation")[:200])
    try:
        keys = []
        for i, f in enumerate(pages, 1):
            p = save_upload(f, d / f"page_{i:02d}", set(IMG_EXT))
            keys.append(keep(jid, f"eval/{jid}/{p.name}", p, IMG_EXT[p.suffix.lower()]) or f"eval/{jid}/{p.name}")
    except HTTPException:
        shutil.rmtree(d, ignore_errors=True)
        with lock:
            jobs.pop(jid, None)
        raise
    update(jid, files={"pages": keys})
    light.submit(runner(jid, evaluate_job))
    return {"id": jid}


def eval_view(j: dict) -> dict:
    out = {k: v for k, v in j.items() if k not in ("result", "files")}
    if j.get("result"):
        out["result"] = grading.apply_overrides(j["result"], j.get("overrides", {}))
    out["page_urls"] = [f"/api/eval/jobs/{j['id']}/pages/{i}" for i in range(1, len(j.get("files", {}).get("pages", [])) + 1)]
    return out


@app.get("/api/eval/jobs")
def list_eval_jobs(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    return page_list(limit, offset, "eval")


@app.get("/api/eval/jobs/{jid}")
def eval_status(jid: str):
    return eval_view(load(jid, "eval"))


@app.post("/api/eval/jobs/{jid}/retry")
def retry_eval(jid: str):
    job = load(jid, "eval")
    if job["stage"] not in ("error", "interrupted"):
        raise HTTPException(409, "Only a failed or interrupted evaluation can be retried.")
    (DATA / jid).mkdir(parents=True, exist_ok=True)
    update(jid, stage="queued", error=None, message="Queued", progress=0.0, owner=INSTANCE)
    light.submit(runner(jid, evaluate_job))
    return {"id": jid}


@app.delete("/api/eval/jobs/{jid}", status_code=204)
def delete_eval(jid: str):
    return delete_job(jid, "eval")


@app.get("/api/eval/jobs/{jid}/pages/{n}")
def eval_page(jid: str, n: int):
    j = load(jid, "eval")
    keys = j.get("files", {}).get("pages", [])
    if not 1 <= n <= len(keys):
        raise HTTPException(404, "No such page.")
    name = Path(keys[n - 1]).name
    return serve(DATA / jid / name, keys[n - 1], IMG_EXT.get(Path(name).suffix.lower(), "image/png"))


class OverrideBody(BaseModel):
    marks: float = Field(ge=0)
    note: str = Field("", max_length=1000)


def _criterion(j: dict, qid: str, cid: str) -> dict:
    if j["stage"] != "done" or not j.get("result"):
        raise HTTPException(409, "Marks can be reviewed once the evaluation has finished.")
    for q in j["result"]["questions"]:
        if q["question_id"] == qid:
            for c in q["criteria"]:
                if c["criterion_id"] == cid:
                    return c
    raise HTTPException(404, f"No criterion {qid}/{cid} in this evaluation.")


@app.put("/api/eval/jobs/{jid}/overrides/{qid}/{cid}")
def set_override(jid: str, qid: str, cid: str, body: OverrideBody):
    j = load(jid, "eval")
    c = _criterion(j, qid, cid)
    if body.marks > c["max_marks"]:
        raise HTTPException(422, f"Marks cannot exceed the rubric maximum of {c['max_marks']:g}.")
    ov = json.loads(json.dumps(j.get("overrides", {})))
    ov.setdefault(qid, {})[cid] = {"marks": round(body.marks, 2), "note": body.note.strip(),
                                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    update(jid, overrides=ov)
    return eval_view(jobs[jid])


@app.delete("/api/eval/jobs/{jid}/overrides/{qid}/{cid}")
def clear_override(jid: str, qid: str, cid: str):
    j = load(jid, "eval")
    _criterion(j, qid, cid)
    ov = json.loads(json.dumps(j.get("overrides", {})))
    ov.get(qid, {}).pop(cid, None)
    update(jid, overrides={q: c for q, c in ov.items() if c})
    return eval_view(jobs[jid])


def _cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s  # spreadsheet formula injection guard


def _csv(rows: list[list], name: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow([_cell(v) for v in r])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


@app.get("/api/eval/export.csv")
def export_all():
    items, _ = store.list_jobs("eval", 100000, 0)
    rows = [["id", "created_utc", "student", "title", "status", "ai_total", "final_total", "max_total", "needs_review", "open_reviews"]]
    rows += [[i["id"], _iso(i["created_at"]), i.get("student"), i["title"], i["stage"], i.get("ai_total"), i.get("final_total"),
              i.get("max_total"), i.get("needs_review"), i.get("open_reviews")] for i in items]
    return _csv(rows, "edusync_evaluations.csv")


@app.get("/api/eval/jobs/{jid}/export.csv")
def export_one(jid: str):
    j = eval_view(load(jid, "eval"))
    if not j.get("result"):
        raise HTTPException(409, "This evaluation has no result yet.")
    rows = [["question_id", "criterion_id", "description", "max_marks", "ai_marks", "second_grader_marks", "teacher_marks", "final_marks",
             "confidence", "needs_review", "evidence_quote", "teacher_note", "flags"]]
    for q in j["result"]["questions"]:
        for c in q["criteria"]:
            rows.append([q["question_id"], c["criterion_id"], c["description"], c["max_marks"], c["marks_awarded"], c.get("second_grader_marks"),
                         c["teacher_marks"], c["final_marks"], c["confidence"], c["needs_review"], c["evidence_quote"], c["teacher_note"], " | ".join(c["flags"])])
    rows.append(["TOTAL", "", j["student"], j["result"]["max_total"], j["result"]["total_score"], "", "", j["result"]["final_total"], "", "", "", "", ""])
    return _csv(rows, f"evaluation_{jid}.csv")


@app.get("/api/eval/sample-rubric")
def sample_rubric():
    return json.loads((Path(__file__).parent / "sample-rubric.json").read_text())


def rubric_file_text(name: str, data: bytes) -> str:
    """Text of an uploaded rubric file (Word, PDF, or any plain-text file); the page parses it with the same rules as typed text."""
    ext = Path(name or "").suffix.lower()
    if ext == ".pdf":
        import fitz
        with fitz.open(stream=data, filetype="pdf") as doc:
            return "\n".join(p.get_text() for p in doc)
    if ext == ".docx":
        import re
        import zipfile
        from xml.sax.saxutils import unescape
        try:
            xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8")
        except (zipfile.BadZipFile, KeyError):
            raise HTTPException(400, "This Word file could not be opened. Save it again as .docx and retry.")
        paras = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
        return "\n".join(unescape("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))) for p in paras)
    if ext in IMG_EXT or ext in {".doc", ".odt", ".rtf", ".pages"}:
        raise HTTPException(415, f"{ext} rubrics are not supported. Upload a .docx, .pdf or .txt file, or paste the text.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(415, "This file is not text. Upload a .docx, .pdf or .txt file, or paste the text.")


@app.post("/api/eval/rubric-text")
async def rubric_text(file: UploadFile = File(...)):
    data = await file.read((5 << 20) + 1)
    if len(data) > 5 << 20:
        raise HTTPException(413, "A rubric file must be under 5 MB.")
    text = rubric_file_text(file.filename or "", data).strip()
    if not text:
        raise HTTPException(400, "No text found in this file. A scanned PDF has no text; type or paste the rubric instead.")
    return {"text": text}


@app.exception_handler(HTTPException)
async def http_err(_, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


if STATIC.exists():
    app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):  # client-side routes (/video, /eval) fall back to index.html
        f = (STATIC / path).resolve()
        return FileResponse(f if path and f.is_file() and STATIC.resolve() in f.parents else STATIC / "index.html")
