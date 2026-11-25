"""Video + slides sync: slides->PNG, Whisper transcript, LLM slide timing, FFmpeg composite."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import pymupdf
from PIL import Image

from . import llm

W, H, FPS = 1280, 720, 30
PIP_W, PIP_H, PIP_M = 360, 270, 20  # presenter window (bottom right) and its margin from the edges
MIN_DWELL = 2.0         # seconds; shorter slide segments are merged into the previous slide
_whisper = None


class VideoError(RuntimeError):
    pass


def run(cmd: list[str], what: str) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise VideoError(f"{what} failed: {p.stderr.strip()[-400:]}")
    return p.stdout


def probe_duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)], "ffprobe")
    try:
        d = float(out.strip())
    except ValueError:
        raise VideoError("Could not read the video duration; is the file a valid video?")
    if d <= 0:
        raise VideoError("Video has zero duration.")
    return d


def has_audio(path: Path) -> bool:
    return bool(run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)], "ffprobe").strip())


# ---------- slides ----------
def slides_to_images(deck: Path, outdir: Path, progress: Callable[[float], None] = lambda f: None) -> tuple[list[Path], list[str]]:
    """PPT/PPTX/PDF -> one PNG per slide (at the output width) + the text of each slide. A PDF skips LibreOffice."""
    outdir.mkdir(parents=True, exist_ok=True)
    pdf = deck
    if deck.suffix.lower() != ".pdf":
        run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(deck)], "PPT conversion (LibreOffice)")
        pdf = outdir / (deck.stem + ".pdf")
        if not pdf.exists():
            raise VideoError("LibreOffice produced no PDF; the presentation file may be corrupt.")
    try:
        doc = pymupdf.open(pdf)
    except Exception as e:
        raise VideoError(f"Could not open the presentation: {e}")
    if doc.page_count == 0:
        raise VideoError("The presentation has no slides.")
    imgs, texts = [], []
    for i, page in enumerate(doc, 1):
        pix = page.get_pixmap(matrix=pymupdf.Matrix(W / page.rect.width, W / page.rect.width))  # the video is W wide: more pixels only cost time
        p = outdir / f"slide_{i:03d}.png"
        pix.save(p)
        imgs.append(p)
        texts.append(" ".join(page.get_text().split()))
        progress(i / doc.page_count)
    return imgs, texts


# ---------- transcript ----------
def _groq_transcribe(video: Path, key: str) -> list[dict]:
    """Hosted Whisper (Groq, free tier): seconds instead of minutes on a weak CPU. Only compressed audio is sent."""
    import tempfile
    import httpx
    with tempfile.TemporaryDirectory() as td:
        audio = Path(td) / "audio.ogg"  # mono 16 kHz Opus ~24 kbps: about 11 MB per hour, under the 25 MB upload limit
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", str(audio)], capture_output=True)
        if r.returncode != 0:
            raise VideoError(f"Audio extraction failed: {r.stderr.decode()[-300:]}")
        if audio.stat().st_size > 24 << 20:
            raise VideoError("audio too long for the hosted transcriber")
        with open(audio, "rb") as f:
            resp = httpx.post("https://api.groq.com/openai/v1/audio/transcriptions", headers={"Authorization": f"Bearer {key}"}, timeout=300,
                              data={"model": os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"), "response_format": "verbose_json", "temperature": "0"},
                              files={"file": ("audio.ogg", f, "audio/ogg")})
    if resp.status_code != 200:
        raise VideoError(f"Groq transcription failed: HTTP {resp.status_code} {resp.text[:200]}")
    segs = resp.json().get("segments") or []
    return [{"start": round(float(s["start"]), 2), "end": round(float(s["end"]), 2), "text": str(s["text"]).strip()} for s in segs if str(s.get("text", "")).strip()]


def transcribe(video: Path) -> list[dict]:
    if key := os.getenv("GROQ_API_KEY"):
        try:
            return _groq_transcribe(video, key)
        except (VideoError, OSError, ValueError, KeyError) as e:  # fall back to the local model; never fail the job over it
            print(f"Hosted transcription unavailable, using local Whisper: {e}", flush=True)
        except Exception as e:  # httpx errors and anything unexpected from the network
            print(f"Hosted transcription unavailable, using local Whisper: {type(e).__name__}", flush=True)
    return _local_transcribe(video)


def _local_transcribe(video: Path) -> list[dict]:
    global _whisper
    from faster_whisper import WhisperModel
    if _whisper is None:
        name = os.getenv("WHISPER_MODEL", "base")
        try:  # cached copy first: no network call, so a flaky connection cannot stall transcription
            _whisper = WhisperModel(name, device="cpu", compute_type="int8", local_files_only=True)
        except Exception:
            _whisper = WhisperModel(name, device="cpu", compute_type="int8")  # not cached yet: download once
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], capture_output=True)
    if raw.returncode != 0:
        raise VideoError(f"Audio extraction failed: {raw.stderr.decode()[-300:]}")
    segs, _ = _whisper.transcribe(np.frombuffer(raw.stdout, dtype=np.float32), vad_filter=True)
    return [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segs if s.text.strip()]


# ---------- matching ----------
def build_match_prompt(slide_texts: list[str], segments: list[dict]) -> str:
    slides = "\n".join(f"Slide {i}: {t[:600] or '(no text, image only)'}" for i, t in enumerate(slide_texts, 1))
    tr = "\n".join(f"#{i} [{s['start']:.1f}s] {s['text']}" for i, s in enumerate(segments))
    return f"""A presenter talks through a slide deck. Decide at what time each slide starts being discussed.
Use meaning (topic, concepts, paraphrase), not just shared words. Slides appear in order unless the speaker clearly goes back.
If the speaker greets or introduces themself before talking about slide 1, slide 1 starts at the first sentence about its content (the earlier part is shown as a full-screen presenter intro). Skip slides the speaker never discusses.

SLIDES:
{slides}

TRANSCRIPT (numbered sentences with start times):
{tr}

Choose the transcript sentence number where the speaker STARTS talking about each slide (the sentence that introduces or transitions into it, not one in the middle of its discussion).
Return JSON: {{"timeline":[{{"slide":1,"segment":0,"confidence":0.0-1.0,"reason":"short"}}]}}
Sorted by segment. confidence = how sure you are the speech at that time is about that slide."""


def segments_to_times(raw: list[dict], segments: list[dict]) -> list[dict]:
    """Model picks a sentence number; the time comes from the transcript, so every cut lands on a sentence boundary."""
    out = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        try:
            i = int(r["segment"])
        except (KeyError, TypeError, ValueError):
            out.append(r)  # tolerate a model that answered with a raw time
            continue
        if 0 <= i < len(segments):
            out.append({**r, "start": segments[i]["start"]})
    return out


def clean_timeline(raw: list[dict], n_slides: int, duration: float) -> list[dict]:
    """Enforce a usable timeline: valid slides, sorted, starts at 0, min dwell, no repeats back-to-back."""
    items = []
    for r in raw:
        try:
            s, t = int(r["slide"]), float(r["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= s <= n_slides and 0 <= t < duration:
            try:
                c = min(max(float(r.get("confidence", 0.5)), 0.0), 1.0)
            except (TypeError, ValueError):
                c = 0.5
            items.append({"slide": s, "start": round(t, 2), "confidence": c, "reason": str(r.get("reason", ""))})
    items.sort(key=lambda x: x["start"])
    out = []
    for it in items:
        if out and it["slide"] == out[-1]["slide"]:
            continue
        if out and it["start"] - out[-1]["start"] < MIN_DWELL:
            continue  # too short to read; keep the earlier slide
        out.append(it)
    if not out:
        return []
    if duration - out[-1]["start"] < MIN_DWELL and len(out) > 1:
        out.pop()
    return out


def even_draft(n_slides: int, duration: float) -> list[dict]:
    step = duration / n_slides
    return [{"slide": i + 1, "start": round(i * step, 2), "confidence": 0.0, "reason": "even split draft, not matched"} for i in range(n_slides)]


def match(slide_texts: list[str], segments: list[dict], duration: float) -> dict:
    """Returns {timeline, weak, note}. weak=True means the UI must ask the human to review."""
    n = len(slide_texts)
    if not segments:
        return {"timeline": even_draft(n, duration), "weak": True, "note": "No speech detected, so slides could not be matched. Edit the timeline below."}
    if not llm.available():
        return {"timeline": even_draft(n, duration), "weak": True, "note": "GEMINI_API_KEY is not set, so automatic matching is off. The timeline below is an even-split draft; edit it, then render."}
    try:
        raw = llm.generate_json(build_match_prompt(slide_texts, segments)).get("timeline", [])
    except llm.LLMError as e:
        return {"timeline": even_draft(n, duration), "weak": True, "note": f"Automatic matching failed ({e}). Edit the timeline below."}
    tl = clean_timeline(segments_to_times(raw, segments), n, duration)
    covered = len({t["slide"] for t in tl})
    mean_conf = sum(t["confidence"] for t in tl) / len(tl) if tl else 0
    if not tl or mean_conf < 0.55 or covered < max(1, n * 0.6):
        return {"timeline": tl or even_draft(n, duration), "weak": True,
                "note": f"Automatic matching is weak (confidence {mean_conf:.0%}, {covered}/{n} slides matched). Please review the timeline."}
    return {"timeline": tl, "weak": False, "note": f"Matched {covered}/{n} slides, mean confidence {mean_conf:.0%}."}


# ---------- background ----------
def detect_background(video: Path, tmp: Path) -> tuple[str | None, str | None]:
    """Find a solid, saturated background colour along the frame border (any hue, not just green).
    Returns (0xRRGGBB, despill type or None). (None, None) when the background is not a solid colour:
    then the presenter is shown as-is rather than cut out badly."""
    import colorsys
    import statistics
    frame = tmp / "frame0.png"
    run(["ffmpeg", "-y", "-v", "error", "-ss", "0.5", "-i", str(video), "-frames:v", "1", str(frame)], "frame grab")
    img = Image.open(frame).convert("RGB")
    w, h = img.size
    k = max(6, min(w, h) // 25)
    spots = [(int(w * f), 2) for f in (0.02, 0.2, 0.4, 0.6, 0.8, 0.98)] + [(2, int(h * f)) for f in (0.2, 0.5, 0.8)] + [(w - k - 2, int(h * f)) for f in (0.2, 0.5, 0.8)]
    px = [img.crop((min(x, w - k), min(y, h - k), min(x, w - k) + k, min(y, h - k) + k)).resize((1, 1), Image.BOX).getpixel((0, 0)) for x, y in spots]
    hsv = [colorsys.rgb_to_hsv(r / 255, g / 255, b / 255) for r, g, b in px]
    sat = statistics.median(v[1] for v in hsv)
    hues = [v[0] * 360 for v in hsv]
    med_h = statistics.median(hues)
    spread = max(min(abs(x - med_h), 360 - abs(x - med_h)) for x in hues)  # lighting changes brightness, not hue
    if sat < 0.3 or spread > 25:
        return None, None
    r, g, b = (int(statistics.median(p[i] for p in px)) for i in range(3))
    # FFmpeg's chromakey compares chroma strength, which shrinks with brightness: a dark backdrop colour would
    # sit next to black clothing and key it out too. Brighten the key (same hue and saturation) to a fixed level.
    f = 160 / max(r, g, b, 1)
    r, g, b = (min(255, round(c * f)) for c in (r, g, b))
    despill = "green" if 70 <= med_h < 190 else "blue" if 190 <= med_h < 270 else None
    return f"0x{r:02x}{g:02x}{b:02x}", despill


# ---------- presenter window placement ----------
PIP_OUTER_W, PIP_OUTER_H = PIP_W + 8, PIP_H + 8   # window plus its border
PIP_X, PIP_Y = W - PIP_OUTER_W - PIP_M, H - PIP_OUTER_H - PIP_M   # always bottom-right


def content_mask(path: Path) -> Image.Image:
    """Half-resolution mask of everything on the slide that differs from its background (text, images, shapes),
    laid out on the output canvas exactly as FFmpeg will place it."""
    from PIL import ImageChops, ImageFilter
    src = Image.open(path).convert("RGB")
    src.thumbnail((W // 2, H // 2))
    canvas = Image.new("RGB", (W // 2, H // 2), "white")
    canvas.paste(src, ((W // 2 - src.width) // 2, (H // 2 - src.height) // 2))
    bg = max(canvas.getcolors(maxcolors=W * H)) [1]  # most common colour = the slide background
    diff = ImageChops.difference(canvas, Image.new("RGB", canvas.size, bg)).convert("L")
    return diff.point(lambda v: 255 if v > 40 else 0).filter(ImageFilter.MaxFilter(9))  # dilate: keep a little air around text


def fit_scale(path: Path) -> float:
    """Largest scale (<= 1) at which this slide, pinned to the top-left, has no content under the presenter window.
    A clear slide stays full size. At scale s the window covers slide area window/s, so growing it past the slide's
    right edge (s <= ~0.69) always succeeds."""
    m = content_mask(path)
    pad = 6
    x0, y0, x1, y1 = PIP_X - pad, PIP_Y - pad, PIP_X + PIP_OUTER_W + pad, PIP_Y + PIP_OUTER_H + pad
    s = 1.0
    while s > 0.4:
        box = (int(x0 / s) // 2, int(y0 / s) // 2, min(W, int(x1 / s) + 1) // 2, min(H, int(y1 / s) + 1) // 2)
        if box[0] >= box[2] or box[1] >= box[3] or m.crop(box).histogram()[255] <= 3:
            return round(s, 2)
        s -= 0.02
    return 0.4


# ---------- render ----------
def slide_chain(n_clips: int, bounds: list[float], total: float, fade: float) -> str:
    """xfade chain for the slide track. bounds = start times of clips 2..n."""
    if n_clips == 1:
        return "[s0]null[slides]"
    parts, prev = [], "s0"
    for k in range(1, n_clips):
        out = "slides" if k == n_clips - 1 else f"x{k}"
        parts.append(f"[{prev}][s{k}]xfade=transition=fade:duration={fade}:offset={bounds[k - 1]:.3f}[{out}]")
        prev = out
    return ";".join(parts)


def render(video: Path, slide_imgs: list[Path], timeline: list[dict], out: Path, work: Path, chroma: str = "auto",
           fade: float = 0.5, progress: Callable[[float, str], None] = lambda p, m: None) -> dict:
    """Layout: before the first slide the presenter plays full-frame, background untouched (intro).
    From the first slide on: the slide fills the frame and the presenter sits in a white window at the bottom right
    (picture-in-picture) with the original background replaced by white."""
    duration = probe_duration(video)
    if not has_audio(video):
        raise VideoError("The video has no audio track, so it cannot be synced to slides.")
    tl = sorted(timeline, key=lambda x: x["start"])
    if not tl:
        raise VideoError("Timeline is empty.")
    for t in tl:
        if not 1 <= t["slide"] <= len(slide_imgs):
            raise VideoError(f"Timeline refers to slide {t['slide']} but the deck has {len(slide_imgs)} slides.")
        if not 0 <= t["start"] < duration:
            raise VideoError(f"Slide {t['slide']} starts at {t['start']}s, outside the video (0 to {duration:.1f}s).")
    intro = tl[0]["start"]  # seconds of full-frame presenter before the first slide (0 = no intro)
    gaps = [b["start"] - a["start"] for a, b in zip(tl, tl[1:])] + ([intro] if intro > 0 else [])
    fade = min(fade, max(0.1, min(gaps or [fade]) - 0.05))

    key, despill = None, None
    if chroma != "off":
        key, despill = detect_background(video, work)
        if key is None and chroma == "on":
            key, despill = "0x00ff00", "green"
    n = len(tl)
    pw, ph = PIP_W, PIP_H
    scales = {p: fit_scale(p) for p in set(slide_imgs[t["slide"] - 1] for t in tl)}   # per slide: shrink only if text would be covered
    starts = [0.0] + [t["start"] for t in tl[1:]]  # the slide track itself runs from 0; the intro covers it
    inputs = ["-i", str(video)]
    fc = []
    for i, t in enumerate(tl):
        # clip i starts on the output timeline at starts[i] and must outlast its own fade-out
        end = (starts[i + 1] + fade) if i + 1 < n else duration
        length = end - starts[i]
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{max(length, 0.1):.3f}", "-i", str(slide_imgs[t["slide"] - 1])]
        sc = scales[slide_imgs[t["slide"] - 1]]
        shrink = "" if sc >= 1 else f",scale={int(W * sc) // 2 * 2}:{int(H * sc) // 2 * 2},pad={W}:{H}:0:0:white"  # pinned top-left, away from the window
        fc.append(f"[{i + 1}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:white{shrink},setsar=1,fps={FPS},format=yuv420p[s{i}]")
    fc.append(slide_chain(n, starts[1:], duration, fade))
    fc.append(f"[0:v]fps={FPS},split=2[pa][pb]" if intro > 0 else f"[0:v]fps={FPS}[pa]")
    # centre-crop the presenter to the window's aspect, then (optionally) remove the background
    crop = f"crop='min(iw,ih*{pw}/{ph})':'min(ih,iw*{ph}/{pw})',scale={pw}:{ph}"
    if key:
        ds = f",despill=type={despill}:mix=0.6:expand=0.2" if despill else ""
        fc.append(f"[pa]{crop},chromakey={key}:0.12:0.06{ds},format=yuva420p[pres]")
    else:
        fc.append(f"[pa]{crop},format=yuv420p[pres]")
    fc.append(f"color=c=white:s={pw}x{ph}:r={FPS}:d={duration:.3f}[box]")
    fc.append(f"[box][pres]overlay=0:0:format=auto,pad={pw + 8}:{ph + 8}:4:4:color=0xcbd5e1[pipv]")
    fc.append(f"[slides][pipv]overlay=x={PIP_X}:y={PIP_Y}:format=auto[b]")
    last = "b"
    if intro > 0:
        fc.append(f"[pb]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,format=yuva420p,"
                  f"fade=t=out:st={intro:.3f}:d={fade}:alpha=1[intro]")
        fc.append(f"[b][intro]overlay=0:0:enable='lte(t,{intro + fade:.3f})':format=auto[c]")
        last = "c"
    fc.append(f"[{last}]format=yuv420p[v]")
    cmd = ["ffmpeg", "-y", "-v", "error", "-progress", "pipe:1", "-nostats", *inputs,
           "-filter_complex", ";".join(fc), "-map", "[v]", "-map", "0:a:0",
           "-c:v", "libx264", "-preset", os.getenv("FFMPEG_PRESET", "veryfast"), "-crf", "22", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-t", f"{duration:.3f}", "-movflags", "+faststart", str(out)]
    progress(0.02, "Rendering video")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in p.stdout:
        m = re.match(r"out_time_us=(\d+)", line)
        if m:
            progress(min(0.98, int(m.group(1)) / 1e6 / duration), "Rendering video")
    err = p.stderr.read()
    if p.wait() != 0:
        raise VideoError(f"FFmpeg render failed: {err.strip()[-500:]}")
    return {"duration": duration, "chroma_key": key, "fade": fade, "slides_used": n, "intro_seconds": round(intro, 2), "presenter_window": "bottom-right", "shrunk_slides": sum(1 for v in scales.values() if v < 1), "min_slide_scale": min(scales.values())}
