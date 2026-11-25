"""LLM client, JSON in/out: Gemini (free tier, key from env) or a local Ollama model (no key, no quota)."""
import base64
import json
import os
import re
import time

import httpx


class LLMError(RuntimeError):
    pass


def _model() -> str:
    # comma-separated: when one model's free daily quota is used up, the next one is tried
    return os.getenv("GEMINI_MODEL", "gemini-2.5-flash,gemini-flash-latest,gemini-flash-lite-latest")


def default_provider() -> str:
    return os.getenv("LLM_PROVIDER", "gemini").lower()


def available() -> bool:
    return default_provider() in ("ollama", "auto") or bool(os.getenv("GEMINI_API_KEY"))


def _ollama(prompt: str, images: list[tuple[bytes, str]] | None) -> dict:
    """Local model through Ollama's native API. Pages are downscaled: CPU inference time grows with pixels."""
    import io
    from PIL import Image
    b64 = []
    for data, _ in images or []:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((1600, 1600))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        b64.append(base64.b64encode(buf.getvalue()).decode())
    body = {"model": os.getenv("OLLAMA_MODEL", "qwen2.5vl:7b"), "stream": False, "format": "json",
            "options": {"temperature": 0, "num_ctx": 8192},
            "messages": [{"role": "user", "content": prompt, **({"images": b64} if b64 else {})}]}
    try:
        r = httpx.post(os.getenv("OLLAMA_URL", "http://localhost:11434") + "/api/chat", json=body, timeout=1800)
    except httpx.HTTPError as e:
        raise LLMError(f"Cannot reach Ollama at {os.getenv('OLLAMA_URL', 'http://localhost:11434')} ({e}). Is `ollama serve` running?")
    if r.status_code != 200:
        raise LLMError(f"Ollama error {r.status_code}: {r.text[:300]} (model pulled? `ollama pull {body['model']}`)")
    return parse_json(r.json()["message"]["content"])


def generate_json(prompt: str, images: list[tuple[bytes, str]] | None = None, retries: int = 6, provider: str | None = None) -> dict:
    """Return the parsed JSON object from the chosen provider. images = [(bytes, mime)]."""
    prov = provider or default_provider()
    if prov == "ollama":
        return _ollama(prompt, images)
    if prov == "auto":  # Gemini first; if it is out of quota or down, use the local model
        try:
            return generate_json(prompt, images, retries, provider="gemini")
        except LLMError:
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
    for model in [m.strip() for m in _model().split(",") if m.strip()]:
        try:
            return _call(model, body, key, retries)
        except _DailyQuota as e:
            last = str(e)
    raise LLMError(f"Every configured Gemini model is out of free daily quota or retired (last tried: {last}). It resets within 24 hours; set GEMINI_MODEL to another model or use a different key.")


class _DailyQuota(Exception):
    pass


def _call(model: str, body: dict, key: str, retries: int) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    last = ""
    for attempt in range(retries):
        try:
            r = httpx.post(url, json=body, headers={"x-goog-api-key": key}, timeout=180)
        except httpx.HTTPError as e:
            last = f"network error: {e}"
            time.sleep(2 ** attempt)
            continue
        if (r.status_code == 429 and "PerDay" in r.text) or r.status_code == 404:  # quota used up or model retired
            raise _DailyQuota(f"{model}: HTTP {r.status_code} {r.text[:160]}")
        if r.status_code in (500, 503) and attempt >= 1:
            raise _DailyQuota(f"{model}: HTTP {r.status_code}")  # overloaded: move on to the next model instead of waiting
        if r.status_code in (429, 500, 503):
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


def parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        out = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"Model returned invalid JSON: {e}")
    if not isinstance(out, dict):
        raise LLMError("Model returned JSON that is not an object")
    return out
