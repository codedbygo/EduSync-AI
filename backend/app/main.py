"""EduSync AI API. Two features, one in-memory job store, a static frontend."""
import json
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import grading, llm, video

_env = Path(__file__).resolve().parents[2] / ".env"  # local dev: KEY=value lines, real env vars win
if _env.exists():
    for _l in _env.read_text().splitlines():
        if "=" in _l and not _l.lstrip().startswith("#"):
            _k, _v = _l.split("=", 1)
            if _v.strip():
                os.environ.setdefault(_k.strip(), _v.strip())

DATA = Path(os.getenv("DATA_DIR", "/tmp/edusync"))
MAX_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
JOB_TTL = 6 * 3600
STATIC = Path(os.getenv("STATIC_DIR", Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"))

app = FastAPI(title="EduSync AI")
heavy = ThreadPoolExecutor(max_workers=1)  # whisper + ffmpeg are CPU bound: one at a time
light = ThreadPoolExecutor(max_workers=4)  # LLM calls are network bound
jobs: dict[str, dict] = {}
lock = threading.Lock()

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
DECK_EXT = {".pptx", ".ppt", ".pdf", ".odp"}
IMG_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def new_job(kind: str) -> tuple[str, Path]:
    now = time.time()
    with lock:
        for jid in [j for j, v in jobs.items() if now - v["created"] > JOB_TTL]:
            shutil.rmtree(DATA / jid, ignore_errors=True)
            jobs.pop(jid)
        jid = uuid.uuid4().hex[:12]
        jobs[jid] = {"id": jid, "kind": kind, "stage": "queued", "progress": 0.0, "message": "Queued", "error": None, "created": now}
    d = DATA / jid
    d.mkdir(parents=True, exist_ok=True)
    return jid, d


def update(jid: str, **kw):
    with lock:
        jobs[jid].update(kw)


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


def runner(jid: str, fn, *args):
    def go():
        try:
            fn(jid, *args)
        except (video.VideoError, llm.LLMError, grading.RubricError) as e:
            update(jid, stage="error", error=str(e), message="Failed")
        except Exception as e:  # last resort: surface it, never hang the UI on "running"
            update(jid, stage="error", error=f"Unexpected error: {type(e).__name__}: {e}", message="Failed")
    return go


@app.get("/api/health")
def health():
    return {"ok": True, "llm_configured": llm.available(), "model": llm._model()}


# ---------------- Feature 1: video + slides ----------------
def analyse(jid: str, auto_render: bool):
    d = DATA / jid
    vpath = next(d.glob("video.*"))
    deck = next(d.glob("deck.*"))
    update(jid, stage="slides", progress=0.05, message="Converting slides to images")
    imgs, texts = video.slides_to_images(deck, d / "slides")
    update(jid, n_slides=len(imgs), slide_texts=texts, stage="transcribing", progress=0.15, message="Transcribing speech (Whisper)")
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
    d = DATA / jid
    update(jid, stage="rendering", progress=0.0, message="Rendering video", result=None)
    info = video.render(next(d.glob("video.*")), sorted((d / "slides").glob("slide_*.png")), timeline, d / "result.mp4", d, chroma=chroma,
                        progress=lambda p, m: update(jid, progress=0.7 + 0.3 * p))
    update(jid, stage="done", progress=1.0, message="Done", result=info, timeline=timeline)


@app.post("/api/video/jobs")
def create_video_job(video_file: UploadFile = File(...), deck_file: UploadFile = File(...), auto_render: bool = Form(True)):
    jid, d = new_job("video")
    try:
        save_upload(video_file, d / "video", VIDEO_EXT)
        save_upload(deck_file, d / "deck", DECK_EXT)
    except HTTPException:
        shutil.rmtree(d, ignore_errors=True)
        jobs.pop(jid, None)
        raise
    heavy.submit(runner(jid, analyse, auto_render))
    return {"id": jid}


@app.post("/api/video/jobs/{jid}/render")
def rerender(jid: str, body: dict):
    job = get_job(jid, "video")
    if job["stage"] in ("slides", "transcribing", "matching", "rendering", "queued"):
        raise HTTPException(409, "Job is still busy.")
    tl = body.get("timeline")
    if not isinstance(tl, list) or not tl:
        raise HTTPException(422, "timeline must be a non-empty list of {slide, start}.")
    try:
        tl = [{"slide": int(t["slide"]), "start": float(t["start"])} for t in tl]
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "Each timeline entry needs a numeric slide and start.")
    chroma = body.get("chroma", "auto")
    if chroma not in ("auto", "on", "off"):
        raise HTTPException(422, "chroma must be auto, on or off.")
    update(jid, stage="rendering", error=None)
    heavy.submit(runner(jid, render_job, tl, chroma))
    return {"id": jid}


def get_job(jid: str, kind: str) -> dict:
    job = jobs.get(jid)
    if not job or job["kind"] != kind:
        raise HTTPException(404, "Job not found (it may have expired).")
    return job


@app.get("/api/video/jobs/{jid}")
def video_status(jid: str):
    with lock:
        job = dict(get_job(jid, "video"))
    job["slide_urls"] = [f"/api/video/jobs/{jid}/slides/{i}" for i in range(1, job.get("n_slides", 0) + 1)]
    job.pop("slide_texts", None)
    return job


@app.get("/api/video/jobs/{jid}/slides/{n}")
def slide_image(jid: str, n: int):
    get_job(jid, "video")
    p = DATA / jid / "slides" / f"slide_{n:03d}.png"
    if not p.exists():
        raise HTTPException(404, "No such slide.")
    return FileResponse(p, media_type="image/png")


@app.get("/api/video/jobs/{jid}/source")
def source_video(jid: str):
    get_job(jid, "video")
    return FileResponse(next((DATA / jid).glob("video.*")))


@app.get("/api/video/jobs/{jid}/result.mp4")
def result_video(jid: str, download: bool = False):
    get_job(jid, "video")
    p = DATA / jid / "result.mp4"
    if not p.exists():
        raise HTTPException(404, "Video not rendered yet.")
    return FileResponse(p, media_type="video/mp4", filename="edusync_lecture.mp4" if download else None)


# ---------------- Feature 2: handwritten evaluation ----------------
def evaluate_job(jid: str, rubric: dict, pages: list[tuple[bytes, str]]):
    update(jid, stage="running", progress=0.05, message="Starting")
    res = grading.evaluate(rubric, pages, lambda p, m: update(jid, progress=p, message=m))
    update(jid, stage="done", progress=1.0, message="Done", result=res)


@app.post("/api/eval/jobs")
def create_eval_job(rubric: str = Form(...), pages: list[UploadFile] = File(...)):
    try:
        rub = json.loads(rubric)
        grading.validate_rubric(rub)
    except json.JSONDecodeError as e:
        raise HTTPException(422, f"Rubric is not valid JSON: {e}")
    except grading.RubricError as e:
        raise HTTPException(422, str(e))
    if not llm.available():
        raise HTTPException(503, "GEMINI_API_KEY is not set on the server, so handwriting cannot be read. See README.")
    if len(pages) > 12:
        raise HTTPException(413, "At most 12 pages per submission.")
    data = []
    for f in pages:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in IMG_EXT:
            raise HTTPException(415, f"'{f.filename}': upload PNG, JPG or WebP images.")
        b = f.file.read(MAX_MB << 20)
        if not b:
            raise HTTPException(400, f"'{f.filename}' is empty.")
        data.append((b, IMG_EXT[ext]))
    jid, _ = new_job("eval")
    light.submit(runner(jid, evaluate_job, rub, data))
    return {"id": jid}


@app.get("/api/eval/jobs/{jid}")
def eval_status(jid: str):
    with lock:
        return dict(get_job(jid, "eval"))


@app.get("/api/eval/sample-rubric")
def sample_rubric():
    return json.loads((Path(__file__).parent / "sample-rubric.json").read_text())


@app.exception_handler(HTTPException)
async def http_err(_, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


if STATIC.exists():
    app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):  # client-side routes (/video, /eval) fall back to index.html
        f = (STATIC / path).resolve()
        return FileResponse(f if path and f.is_file() and STATIC.resolve() in f.parents else STATIC / "index.html")
