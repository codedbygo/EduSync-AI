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


def generate_json(prompt: str, images: list[tuple[bytes, str]] | None = None, retries: int = 6, provider: str | None = None) -> dict:
    """Return the parsed JSON object from the chosen provider. images = [(bytes, mime)]."""
    prov = provider or default_provider()
    if prov == "ollama":
        return _ollama(prompt, images)
    if prov == "openrouter":
        return _openrouter(prompt, images)
    if prov == "auto":  # Gemini first; then OpenRouter's free models (if a key is set); last, the local model
        try:
            return generate_json(prompt, images, retries, provider="gemini")
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
    if isinstance(out, list):  # some models answer with the bare list instead of {"key": [...]}
        out = {"items": out}
    if not isinstance(out, dict):
        raise LLMError("Model returned JSON that is not an object")
    return out
