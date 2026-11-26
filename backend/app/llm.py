"""LLM client, JSON in/out: Gemini (free tier, key from env) or a local Ollama model (no key, no quota)."""
import base64
import json
import logging
import os
import re
import time

import httpx

log = logging.getLogger("edusync.llm")


class LLMError(RuntimeError):
    pass


class ParseError(LLMError):
    """The model answered, but not with usable JSON: worth one retry with a stricter prompt."""


def _model() -> str:
    # comma-separated: when one model's free daily quota is used up, the next one is tried
    return os.getenv("GEMINI_MODEL", "gemini-3.5-flash,gemini-3.6-flash,gemini-3-flash-preview,gemini-3.1-flash-lite,gemini-flash-lite-latest")


def default_provider() -> str:
    return os.getenv("LLM_PROVIDER", "gemini").lower()


def available() -> bool:
    return default_provider() in ("ollama", "auto") or bool(os.getenv("GEMINI_API_KEY") or os.getenv("OPENROUTER_API_KEY"))


def _jpegs(images: list[tuple[bytes, str]] | None) -> list[str]:
    """Pages downscaled to 1600px JPEG, base64: smaller uploads, and CPU inference time grows with pixels."""
    import io
    from PIL import Image
    out = []
    for data, _ in images or []:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((1600, 1600))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        out.append(base64.b64encode(buf.getvalue()).decode())
    return out


def _openrouter(prompt: str, images: list[tuple[bytes, str]] | None) -> dict:
    """OpenRouter free models (e.g. NVIDIA Nemotron VL). OpenAI-style API; a model that is rate limited or down is skipped."""
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise LLMError("OPENROUTER_API_KEY is not set. Free key (no card): https://openrouter.ai/keys")
    content = [{"type": "text", "text": prompt}] + [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}} for b in _jpegs(images)]
    last = ""
    for model in [m.strip() for m in os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3.5-super-vl:free,nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free,google/gemma-4-31b-it:free").split(",") if m.strip()]:
        body = {"model": model, "temperature": 0, "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": content}]}
        try:
            r = httpx.post("https://openrouter.ai/api/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"}, timeout=180)
        except httpx.HTTPError as e:
            last = f"{model}: network error {e}"
            continue
        if r.status_code != 200:
            last = f"{model}: HTTP {r.status_code} {r.text[:120]}"
            continue
        try:
            return parse_json(r.json()["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, ValueError, TypeError, LLMError) as e:
            last = f"{model}: unusable reply ({type(e).__name__})"
    raise LLMError(f"Every OpenRouter model failed (last: {last})")


def _ollama(prompt: str, images: list[tuple[bytes, str]] | None) -> dict:
    """Local model through Ollama's native API."""
    b64 = _jpegs(images)
    # stream: tokens flow as they are made, so a tunnel/proxy (Cloudflare cuts silent requests at ~100 s) never sees an idle connection
    body = {"model": os.getenv("OLLAMA_MODEL", "qwen2.5vl:7b"), "stream": True, "format": "json", "keep_alive": "30m",
            "options": {"temperature": 0, "num_ctx": 8192},
            "messages": [{"role": "user", "content": prompt, **({"images": b64} if b64 else {})}]}
    url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    try:
        with httpx.stream("POST", url + "/api/chat", json=body, timeout=httpx.Timeout(1800, connect=20)) as r:
            if r.status_code != 200:
                r.read()
                raise LLMError(f"Ollama error {r.status_code}: {r.text[:300]} (model pulled? `ollama pull {body['model']}`)")
            text = ""
            for line in r.iter_lines():
                if line.strip():
                    part = json.loads(line)
                    if part.get("error"):
                        raise LLMError(f"Ollama error: {part['error']}")
                    text += part.get("message", {}).get("content", "")
    except httpx.HTTPError as e:
        raise LLMError(f"Cannot reach Ollama at {url} ({e}). Is `ollama serve` running and the tunnel up?")
    return parse_json(text)


def generate_json(prompt: str, images: list[tuple[bytes, str]] | None = None, retries: int = 6, provider: str | None = None,
                  models: str | None = None) -> dict:
    """Return the parsed JSON object from the chosen provider. images = [(bytes, mime)].
    models: Gemini models to try, in order (comma separated); default GEMINI_MODEL."""
    prov = provider or default_provider()
    if prov == "ollama":
        return _ollama(prompt, images)
    if prov == "openrouter":
        return _openrouter(prompt, images)
    if prov == "auto":  # Gemini first; then OpenRouter's free models (if a key is set); last, the local model
        try:
            return generate_json(prompt, images, retries, provider="gemini", models=models)
        except LLMError:
            pass
        if os.getenv("OPENROUTER_API_KEY"):
            try:
                return _openrouter(prompt, images)
            except LLMError:
                pass
        return _ollama(prompt, images)
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise LLMError("GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com/apikey and put it in .env")
    parts: list[dict] = [{"text": prompt}]
    for data, mime in images or []:
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    last = ""
    for model in [m.strip() for m in (models or _model()).split(",") if m.strip()]:
        try:
            return _call(model, body, key, retries)
        except _DailyQuota as e:
            last = str(e)
    raise LLMError(f"Every configured Gemini model is busy, out of free quota or retired (last tried: {last}). Press Retry in a few minutes.")


class _DailyQuota(Exception):
    pass


def _call(model: str, body: dict, key: str, retries: int) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    last = ""
    for attempt in range(retries):
        try:
            r = httpx.post(url, json=body, headers={"x-goog-api-key": key}, timeout=httpx.Timeout(120, connect=15))
        except httpx.TimeoutException:
            raise _DailyQuota(f"{model}: timed out")  # a stuck model: try the next one instead of waiting again
        except httpx.HTTPError as e:
            last = f"network error: {e}"
            time.sleep(2 ** attempt)
            continue
        if (r.status_code == 429 and "PerDay" in r.text) or r.status_code == 404:  # quota used up or model retired
            raise _DailyQuota(f"{model}: HTTP {r.status_code} {r.text[:160]}")
        if r.status_code in (500, 503):
            raise _DailyQuota(f"{model}: HTTP {r.status_code}")  # overloaded: move on to the next model at once
        if r.status_code == 429:
            if attempt >= 1:
                raise _DailyQuota(f"{model}: HTTP 429 twice")  # still rate limited: the next model has its own quota
            last = f"HTTP {r.status_code} (rate limit or overload)"
            time.sleep(min(60, 15 * (attempt + 1)))  # free tier: per-minute quota resets within a minute
            continue
        if r.status_code != 200:
            raise LLMError(f"Gemini API error {r.status_code}: {r.text[:300]}")
        try:
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, ValueError):
            raise LLMError(f"Gemini returned no content: {r.text[:300]}")
        return parse_json(text)
    raise LLMError(f"Gemini unavailable after {retries} tries: {last}")


def embed(texts: list[str]) -> list[list[float]]:
    """Meaning vectors for texts (Gemini embeddings, free tier): similar meaning, nearby vectors, whatever the wording.
    Sent in batches of 100 in parallel; any failure raises LLMError so the caller can fall back to word matching."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise LLMError("GEMINI_API_KEY is not set")
    model = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"

    def batch(chunk: list[str]) -> list[list[float]]:
        body = {"requests": [{"model": f"models/{model}", "content": {"parts": [{"text": t[:2000] or "(empty)"}]}} for t in chunk]}
        for attempt in range(3):
            try:
                r = httpx.post(url, json=body, headers={"x-goog-api-key": key}, timeout=httpx.Timeout(30, connect=10))
            except httpx.HTTPError as e:
                err = f"network error: {e}"
            else:
                if r.status_code == 200:
                    return [e["values"] for e in r.json()["embeddings"]]
                err = f"HTTP {r.status_code} {r.text[:120]}"
                if r.status_code not in (429, 500, 503):
                    break
            time.sleep(2 * (attempt + 1))
        raise LLMError(f"Embedding failed: {err}")

    from concurrent.futures import ThreadPoolExecutor
    chunks = [texts[i:i + 100] for i in range(0, len(texts), 100)]
    with ThreadPoolExecutor(4) as ex:
        return [v for part in ex.map(batch, chunks) for v in part]


def describe_image(data: bytes, mime: str) -> str:
    """A one-line description of a slide image, for slides with no text to match against. One fast try only."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise LLMError("GEMINI_API_KEY is not set")
    model = os.getenv("GEMINI_FAST_MODEL", "gemini-flash-lite-latest")
    body = {"contents": [{"parts": [{"text": "This is a lecture slide. In one line, give its title and the key topics or labels it shows. Plain text only."},
                                    {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}]}],
            "generationConfig": {"temperature": 0}}
    try:
        r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", json=body,
                       headers={"x-goog-api-key": key}, timeout=httpx.Timeout(25, connect=10))
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip() if r.status_code == 200 else ""
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return ""


def parse_json(text: str) -> dict:
    """The first JSON object or array in the reply, with code fences and any prose around it ignored.
    A bare array comes back as {"items": [...]}."""
    body = re.sub(r"```(?:json)?", "", text or "", flags=re.I)
    starts = [i for i in (body.find("{"), body.find("[")) if i >= 0]
    try:
        if not starts:
            raise ValueError("no JSON object or array in the reply")
        out, _ = json.JSONDecoder().raw_decode(body[min(starts):])
    except ValueError as e:  # JSONDecodeError is a ValueError
        log.warning("Unparseable model reply (%s): %s", e, (text or "")[:4000])
        raise ParseError(f"Model returned invalid JSON: {e}")
    return {"items": out} if isinstance(out, list) else out
