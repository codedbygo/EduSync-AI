"""Video + slides sync: slides->PNG, Whisper transcript, LLM slide timing, FFmpeg composite."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Callable

import numpy as np
import pymupdf
from PIL import Image

from . import llm

W, H, FPS = 1280, 720, 30
PIP_W, PIP_H, PIP_M = 360, 270, 20  # presenter window (bottom right) and its margin from the edges
MIN_DWELL = 2.0         # seconds; shorter slide segments are merged into the previous slide
_whisper = None
_ai_pool = ThreadPoolExecutor(2)  # AI matching runs here so the job can stop waiting at the deadline


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
def build_match_prompt(slide_texts: list[str], segments: list[dict], known: dict[int, int] | None = None) -> str:
    """known: deck slide -> transcript sentence already fixed by a spoken cue; the model is asked only for the rest."""
    slides = "\n".join(f"Slide {i}: {t[:600] or '(no text, image only)'}" for i, t in enumerate(slide_texts, 1))
    tr = "\n".join(f"#{i} [{s['start']:.1f}s] {s['text']}" for i, s in enumerate(segments))
    want = [k for k in range(1, len(slide_texts) + 1) if k not in (known or {})]
    placed = ("\nALREADY PLACED from the speaker's own slide announcements (these deck slide numbers are correct, do not return them):\n"
              + "\n".join(f"Slide {k} starts at sentence #{i}" for k, i in sorted(known.items())) + "\n") if known else ""
    return f"""A presenter talks through a slide deck. Decide at what time each slide starts being discussed.
Use meaning (topic, concepts, paraphrase), not just shared words. Slides appear in order unless the speaker clearly goes back.
If the speaker greets or introduces themself before talking about slide 1, slide 1 starts at the first sentence about its content (the earlier part is shown as a full-screen presenter intro).
A spoken "slide 5" or "next slide" marks a slide change, but the speaker's numbers may not match the deck's numbering (title slides are often not counted).
For a slide the speaker never clearly discusses, pick the most plausible sentence and give it a low confidence.

SLIDES:
{slides}

TRANSCRIPT (numbered sentences with start times):
{tr}
{placed}
Choose the transcript sentence number where the speaker STARTS talking about each slide (the sentence that introduces or transitions into it, not one in the middle of its discussion).
Return exactly one entry for each of these slides: {", ".join(map(str, want))}.
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
    return [{"slide": i + 1, "start": round(i * step, 2), "confidence": 0.0, "reason": "evenly spaced guess, not matched"} for i in range(n_slides)]


# ---------- spoken cues: "Slide 4", "slide six", "next slide" ----------
_NUMS = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
CUE = re.compile(r"\b(?:slide\s+(?:number\s+|no\.?\s*)?(\d{1,3}|" + "|".join(sorted(_NUMS, key=len, reverse=True)) + r")\b|next\s+slide\b)", re.I)
LEAD = 0.5  # seconds: the slide appears just before the cue is spoken
CUE_REASON, GAP_REASON = "spoken cue (\"slide N\" or \"next slide\")", "placed between neighbouring slides (no cue found)"


_STOP = set("this that with from have will what which their they there about into them then than also these those very when where "
             "your said slide here just like know because been were would could should being some such each other more most only over "
             "under after before again now".split())


def _words(text: str) -> set[str]:
    return {w[:-1] if w.endswith("s") else w for w in re.findall(r"[a-z]{4,}", text.lower())} - _STOP


def _cues(segments: list[dict]) -> list[tuple[float, int | None, str]]:
    """(time the cue is spoken, the number said or None for "next slide", what is said right after it: the rest of
    its sentence and the sentences that start within 15 s, at most 3, never past the next cue). Later speech may be
    about a slide that has no cue of its own. The time is estimated inside its sentence (Whisper times sentences)."""
    found = []
    for i, s in enumerate(segments):
        text, t0 = s["text"], float(s["start"])
        span = max(0.0, float(s.get("end", t0)) - t0)
        for m in CUE.finditer(text):
            g = (m.group(1) or "").lower()
            found.append((max(0.0, t0 + span * m.start() / max(len(text), 1) - LEAD), int(g) if g.isdigit() else _NUMS.index(g) if g else None, i, m.end()))
    out = []
    for c, (t, k, i, end) in enumerate(found):
        nxt = found[c + 1][0] if c + 1 < len(found) else float("inf")
        after = [s["text"] for s in segments[i + 1:i + 4] if float(s["start"]) < min(nxt, t + 15)]
        out.append((t, k, " ".join([segments[i]["text"][end:]] + after)))
    return out


def spoken_anchors(segments: list[dict], slide_texts: list[str]) -> dict[int, float]:
    """Deck slide -> time the presenter moves to it, from "slide N" / "next slide" cues.
    The time of a cue is trusted; the number is only a hint. Presenters often number from their first content
    slide (skipping title slides), or count a slide the uploaded deck does not have, so each cue is matched to
    the deck slide whose text best fits what is said after it. Order is kept (deck slides only go up), a steady
    numbering offset is preferred, and a cue that fits no slide (a look back, "as we saw in slide 3") is skipped.
    Image-only decks give no text to compare, so the spoken number is taken literally. Fewer than 2 cues: none."""
    cues, n = _cues(segments), len(slide_texts)
    if len(cues) < 2:
        return {}
    sw = [_words(t) for t in slide_texts]
    fit = [[0.0] + [len(sw[j - 1] & _words(win)) / max(len(sw[j - 1]), 1) for j in range(1, n + 1)] for _, _, win in cues]
    OFFSET_CHANGE, SKIP_CUE = 0.15, 0.1  # a jump the spoken numbers explain is free; changing the offset is not
    NEG = float("-inf")
    # f[i][j]: best score with cue i opening deck slide j as the latest assignment; back[i][j]: (previous cue, slide)
    f = [[NEG] * (n + 1) for _ in cues]
    back: list[list[tuple[int, int] | None]] = [[None] * (n + 1) for _ in cues]
    for i, (_, k, _) in enumerate(cues):
        for j in range(1, n + 1):
            hint = -0.01 * abs(j - k) if k else 0.0
            f[i][j], back[i][j] = fit[i][j] + hint - SKIP_CUE * i, None  # first assigned cue; earlier cues skipped
            for p in range(i):  # O(cues^2 * slides^2): a few hundred thousand steps for a long lecture
                for pj in range(1, j):
                    if f[p][pj] == NEG:
                        continue
                    kp = cues[p][1]
                    steady = (j - pj == 1) if (k is None or kp is None) else (j - k == pj - kp)
                    s = f[p][pj] + fit[i][j] + hint - SKIP_CUE * (i - p - 1) - (0 if steady else OFFSET_CHANGE)
                    if s > f[i][j]:
                        f[i][j], back[i][j] = s, (p, pj)
    end = max(((i, j) for i in range(len(cues)) for j in range(1, n + 1)), key=lambda ij: f[ij[0]][ij[1]] - SKIP_CUE * (len(cues) - 1 - ij[0]))
    out: dict[int, float] = {}
    cur: tuple[int, int] | None = end
    while cur:
        out[cur[1]] = round(cues[cur[0]][0], 2)
        cur = back[cur[0]][cur[1]]
    return out if len(out) >= 2 else {}


def intro_end(segments: list[dict]) -> float:
    """The opening sentence (the greeting) always plays as the full-screen intro, original background and all;
    the slides fade in when it ends. A spoken "slide N" before then still wins."""
    return float(segments[0]["end"]) if segments else 0.0


def fill_timeline(anchors: dict[int, float], proposals: dict[int, tuple[float, float, str]], n: int, duration: float,
                  intro: float = 0.0, backup: dict[int, tuple[float, float, str]] | None = None) -> list[dict]:
    """Cues first; AI proposals only for slides without a cue and only where they fit between the cues around
    them (and not inside the intro); every other slide is spread between its nearest placed neighbours (never
    across the whole video)."""
    known = {k: (t, 1.0, CUE_REASON) for k, t in anchors.items()}
    first = min(known.values(), default=(duration,))[0]
    intro = max(0.0, min(intro, first - MIN_DWELL))  # a cue inside the greeting shortens the intro
    for props in (proposals, backup or {}):  # the backup only fills slides the first set could not place in order
        for k in sorted(props):
            if k in known:
                continue
            lo = max((known[j][0] for j in known if j < k), default=intro - 1e-6)
            hi = min((known[j][0] for j in known if j > k), default=duration)
            if lo < props[k][0] < hi:
                known[k] = props[k]
    known.setdefault(1, (intro, 0.3, GAP_REASON))
    fixed = dict(known)
    for k in range(2, n + 1):
        if k not in fixed:
            a = max(j for j in fixed if j < k)
            b = min((j for j in fixed if j > k), default=n + 1)
            ta, tb = fixed[a][0], fixed[b][0] if b <= n else duration
            known[k] = (ta + (tb - ta) * (k - a) / (b - a), 0.3, GAP_REASON)
    raw = [{"slide": k, "start": t, "confidence": c, "reason": why} for k, (t, c, why) in known.items()]
    return clean_timeline(raw, n, duration)


TEXT_REASON = "matched by what is said"
SWITCH, SKIP = 1.0, 1.5  # path costs, in units of "one sentence clearly about this slide": changing slide, skipping one


_embedder = None


def local_embed(texts: list[str]) -> np.ndarray:
    """Meaning vectors on this machine: a small sentence model (all-MiniLM-L6-v2, quantised ONNX, ~21 MB, fetched
    once from Hugging Face like the Whisper model) run with onnxruntime and tokenizers, which faster-whisper already
    installs. No quota, no network after the first download, ~0.1 s for a lecture."""
    global _embedder
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer
    if _embedder is None:
        repo = os.getenv("EMBED_MODEL_REPO", "Xenova/all-MiniLM-L6-v2")
        tok = Tokenizer.from_file(hf_hub_download(repo, "tokenizer.json"))
        tok.enable_truncation(256)
        tok.enable_padding()
        _embedder = (tok, ort.InferenceSession(hf_hub_download(repo, "onnx/model_quantized.onnx"), providers=["CPUExecutionProvider"]))
    tok, sess = _embedder
    out = []
    for i in range(0, len(texts), 64):
        enc = tok.encode_batch([t or "(empty)" for t in texts[i:i + 64]])
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if any(x.name == "token_type_ids" for x in sess.get_inputs()):
            feed["token_type_ids"] = np.zeros_like(ids)
        h = sess.run(None, feed)[0]
        out.append((h * mask[..., None]).sum(1) / np.maximum(mask.sum(1, keepdims=True), 1))  # mean of the word vectors
    return np.vstack(out)


def similarity(slide_texts: list[str], segments: list[dict]) -> tuple[np.ndarray, str]:
    """Rows: sentences; columns: slides. Meaning similarity from sentence embeddings plus shared-word coverage
    (exact terms such as "ADDIE" or "Kirkpatrick" count strongly); words alone when no embedding is available.
    Each row is standardised (how much more this sentence fits this slide than the others), so a vague sentence
    counts little and a sentence clearly about one slide counts a lot. Sentences are scored on their own: pairing
    one with the next made the slide change a sentence early."""
    windows = [s["text"] for s in segments]
    texts = [t[:1500] for t in slide_texts] + windows
    v = None
    try:
        v = local_embed(texts)
    except Exception as e:  # model download or runtime problem: the hosted embeddings next, then shared words
        print(f"Local embeddings unavailable ({type(e).__name__}: {e}); trying Gemini embeddings", flush=True)
        try:
            v = np.array(llm.embed(texts), dtype=float)
        except llm.LLMError as e2:
            print(f"Embeddings unavailable, matching by shared words: {e2}", flush=True)
    def standard(x: np.ndarray) -> np.ndarray:
        sd = x.std(axis=1, keepdims=True)
        return (x - x.mean(axis=1, keepdims=True)) / np.where(sd > 1e-9, sd, 1.0)

    sw = [_words(t) for t in slide_texts]
    words = standard(np.array([[len(w & _words(win)) / max(len(w), 1) for w in sw] for win in windows], dtype=float))
    if v is None:
        return words, "shared words"
    v = v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)
    return standard(v[len(slide_texts):] @ v[:len(slide_texts)].T) + words, "meaning"


def content_proposals(anchors: dict[int, float], slide_texts: list[str], segments: list[dict], duration: float,
                      intro: float = 0.0) -> tuple[dict[int, tuple[float, float, str]], str]:
    """Match the whole talk to the deck at once, from what is said (no cue words needed): every sentence after the
    intro is given to one slide, slides only move forward, and the path that best fits all sentences together wins
    (changing slide and skipping a slide cost a little, so one stray sentence cannot flip the slide). Spoken cues,
    if any, are fixed points the path must respect. A slide starts at its first sentence.
    Returns (slide -> (start, confidence, reason) for slides without a cue, how the similarity was measured)."""
    n = len(slide_texts)
    idx = [i for i, s in enumerate(segments) if float(s["start"]) >= intro - 1e-6]
    if not idx or n == 0:
        return {}, ""
    z, how = similarity(slide_texts, [segments[i] for i in idx])
    if not np.abs(z).max() > 1e-9:  # nothing said resembles any slide more than another: no basis for a timeline
        return {}, how
    m = len(idx)
    allowed = np.ones((m, n), dtype=bool)  # cue constraints: before slide k's cue, below k; after it, k or above
    for k, t in anchors.items():
        for r, i in enumerate(idx):
            s = segments[i]
            if float(s["start"]) >= t + LEAD - 1e-6:
                allowed[r, :k - 1] = False
            elif float(s.get("end", s["start"])) <= t + LEAD:
                allowed[r, k - 1:] = False
    NEG = -1e18
    j = np.arange(n)
    f = np.where(allowed[0], z[0] - SKIP * j, NEG)  # starting past slide 1 skips the earlier ones
    came = np.zeros((m, n), dtype=int)
    for r in range(1, m):
        best_prev = np.maximum.accumulate(f + SKIP * j)  # best earlier slide, skip cost folded in
        jump = np.full(n, NEG)
        jump[1:] = best_prev[:-1] - SKIP * (j[1:] - 1) - SWITCH
        jump_from = np.zeros(n, dtype=int)
        arg = np.array([int(np.argmax((f + SKIP * j)[:x])) if x else 0 for x in range(n)])
        jump_from[1:] = arg[1:]
        stay_better = f > jump  # on a tie, the slide changes as late as possible: when the speech clearly moves on
        came[r] = np.where(stay_better, j, jump_from)
        f = np.where(allowed[r], z[r] + np.maximum(f, jump), NEG)
    end = int(np.argmax(f - SKIP * (n - 1 - j)))  # stopping before the last slide skips the later ones
    path = [end]
    for r in range(m - 1, 0, -1):
        path.append(int(came[r][path[-1]]))
    path.reverse()
    out: dict[int, tuple[float, float, str]] = {}
    for r, k in enumerate(path):
        slide = k + 1
        if slide not in out and slide not in anchors:
            rows = [q for q, kk in enumerate(path) if kk == k]
            fit = float(np.mean([z[q, k] for q in rows]))  # about 1 = clearly this slide; about 0 = could be any
            out[slide] = (float(segments[idx[r]]["start"]), round(min(0.95, max(0.3, 0.5 + fit / 4)), 2), TEXT_REASON)
    return out, how


# ---------- AI matching for slides without a cue ----------
MATCH_MODELS = os.getenv("GEMINI_MATCH_MODEL", "gemini-3.1-flash-lite,gemini-flash-lite-latest,gemini-3.5-flash,gemini-3-flash-preview")  # fast first
AI_DEADLINE = float(os.getenv("AI_MATCH_SECONDS", "75"))  # the job never waits longer than this for the AI
STRICT = """

Your previous answer could not be used ({problem}). Reply with ONLY this JSON object and nothing else, no code fences:
{{"timeline":[{{"slide":1,"segment":0,"confidence":0.9,"reason":"short"}}]}}
Exactly one entry for each of these slides, in order: {slides}. Segment numbers must only go up."""


def _timeline_items(res: dict) -> list:
    """The list in the reply, whether bare or wrapped as {"timeline": [...]} or under another key."""
    for key in ("timeline", "items", "slides"):
        if isinstance(res.get(key), list):
            return res[key]
    return next((v for v in res.values() if isinstance(v, list)), [])


def check_timeline(items: list, n: int, duration: float, want: set[int] | None = None) -> str | None:
    """None when the answer is usable as given, else what is wrong (sent back to the model on the retry).
    want: the slides that were asked for (default: all of them)."""
    want = want or set(range(1, n + 1))
    try:
        pairs = sorted((int(r["slide"]), float(r["start"])) for r in items)
    except (KeyError, TypeError, ValueError):
        return "every entry needs a slide number and a valid segment number"
    if not pairs:
        return "the timeline was empty"
    if any(s not in want for s, _ in pairs):
        return f"only these slides were asked for: {', '.join(map(str, sorted(want)))}"
    if any(not 0 <= t < duration for _, t in pairs):
        return "a time was outside the video"
    if any(b[1] <= a[1] for a, b in zip(pairs, pairs[1:])):
        return "start times must increase with the slide number"
    if len({s for s, _ in pairs}) != len(want):
        return f"{len({s for s, _ in pairs})} of {len(want)} slides were given, one entry per slide is required"
    return None


def ai_proposals(slide_texts: list[str], segments: list[dict], duration: float,
                 anchors: dict[int, float] | None = None) -> tuple[dict[int, tuple[float, float, str]], str | None]:
    """Ask the model for the slides the cues did not place (all of them when there are no cues); on unusable JSON or
    an answer that breaks the rules, ask once more with a stricter prompt.
    Returns (slide -> (start, confidence, reason), the problem left after the retry or None)."""
    n = len(slide_texts)
    # the sentence each cue falls in, so the model sees the fixed points in its own terms
    known = {k: max((i for i, s in enumerate(segments) if float(s["start"]) <= t + LEAD), default=0) for k, t in (anchors or {}).items()}
    want = set(range(1, n + 1)) - set(known)
    base = build_match_prompt(slide_texts, segments, known)
    prompt, items, problem = base, [], None
    for _ in range(2):
        try:
            items = segments_to_times(_timeline_items(llm.generate_json(prompt, retries=2, models=MATCH_MODELS)), segments)
            problem = check_timeline(items, n, duration, want)
        except llm.ParseError as e:
            items, problem = [], str(e)
        if not problem:
            break
        print(f"Slide matching answer rejected: {problem}", flush=True)
        prompt = base + STRICT.format(problem=problem, slides=", ".join(map(str, sorted(want))))
    usable = clean_timeline(items, n, duration)  # the best of an imperfect answer still helps
    return {t["slide"]: (t["start"], t["confidence"], t["reason"] or "matched by AI") for t in usable}, problem


def guess(n: int, duration: float, why: str) -> dict:
    return {"timeline": even_draft(n, duration), "weak": True,
            "note": f"Could not sync automatically: {why}. The timeline below is only an evenly spaced GUESS, not matched to the speech. "
                    "Play the video, press \"Use current time\" at each slide change, then render."}


def match(slide_texts: list[str], segments: list[dict], duration: float) -> dict:
    """Returns {timeline, weak, note}. Works from what is said; "slide N" cues help but are not needed.
    weak=True (the UI asks a human) only when nothing could be matched at all: otherwise the job renders on its own."""
    n = len(slide_texts)
    if not segments:
        return guess(n, duration, "no speech was detected")
    anchors = spoken_anchors(segments, slide_texts)
    intro = max(0.0, min(intro_end(segments), min(anchors.values(), default=duration) - MIN_DWELL))
    ai_job = None
    if len(anchors) < n * 0.6 and llm.available():  # few cues: ask the AI too, alongside the instant local match
        ai_job = _ai_pool.submit(ai_proposals, slide_texts, segments, duration, anchors)
    proposals, how = content_proposals(anchors, slide_texts, segments, duration, intro)
    backup, ai_note = {}, ""
    if ai_job is not None:
        try:
            ai, problem = ai_job.result(timeout=AI_DEADLINE)
            if ai:  # the AI reads meaning best; the local match fills any slide it left out or put out of order
                proposals, backup = ai, proposals
        except FuturesTimeout:
            ai_note = f"the AI did not answer within {AI_DEADLINE:.0f} s, so the instant match was used"
        except llm.LLMError as e:
            ai_note = f"the AI was unavailable ({e}), so the instant match was used"
    if not anchors and not proposals and not backup:
        return guess(n, duration, ai_note or "nothing in the speech could be matched to a slide")
    tl = fill_timeline(anchors, proposals, n, duration, intro, backup)
    cue = sum(1 for t in tl if t["reason"] == CUE_REASON)
    gap = sum(1 for t in tl if t["reason"] == GAP_REASON)
    txt = sum(1 for t in tl if t["reason"] == TEXT_REASON)
    ai = len(tl) - cue - gap - txt
    parts = ([f"{txt} by the meaning of what is said" if how == "meaning" else f"{txt} by shared words"] * bool(txt)
             + [f"{cue} at spoken \"slide N\" cues"] * bool(cue) + [f"{ai} by AI matching"] * bool(ai)
             + [f"{gap} between neighbouring slides"] * bool(gap))
    note = f"{len(tl)}/{n} slides placed: " + ", ".join(parts) + "."
    less = [t["slide"] for t in tl if t["confidence"] < 0.6]
    if less:
        note += f" Less certain: slide{'s' if len(less) > 1 else ''} {', '.join(map(str, less))}; adjust only if needed."
    if ai_note:
        note += f" Note: {ai_note}."
    return {"timeline": tl, "weak": not tl, "note": note}


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
    # The measured colour itself: render keys by RGB distance (colorkey), so dark clothing and skin, far from
    # the backdrop in RGB, stay solid even when the backdrop is a yellow-green with little chroma.
    r, g, b = (int(statistics.median(p[i] for p in px)) for i in range(3))
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
        ds = f",despill=type={despill}:mix=0.3:expand=0" if despill else ""
        # ponytail: one global RGB distance; a backdrop lit very unevenly may need a per-video similarity knob
        fc.append(f"[pa]{crop},format=rgba,colorkey={key}:0.12:0.02{ds},format=yuva420p[pres]")
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
