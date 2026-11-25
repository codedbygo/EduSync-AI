---
title: EduSync AI
sdk: docker
app_port: 7860
---

# EduSync AI

Two tools in one app:

1. **Video + Slides Sync.** Raw presenter video (green screen optional) plus a PPT/PDF becomes one MP4: presenter on the right over a clean white background, the slide that is being discussed on the left, crossfades between slides, original audio untouched.
2. **Handwritten Answer Evaluation.** Answer-sheet images plus questions and a rubric produce criterion-by-criterion marks with reasoning, quoted evidence, missing concepts, incorrect statements and a human-review flag.

Stack: FastAPI, faster-whisper (local), FFmpeg, LibreOffice, PyMuPDF, Gemini free tier (matching, OCR, grading). Frontend: React 19, TypeScript, Vite, Tailwind, shadcn/ui, TanStack Query and Router, Zod.

## Run locally

```bash
cp .env.example .env            # add a free GEMINI_API_KEY (https://aistudio.google.com/apikey, no card)
# backend (needs ffmpeg and libreoffice on PATH)
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8000
# frontend (second terminal)
cd frontend && pnpm install && pnpm dev     # http://localhost:5173, proxies /api to :8000
```

Or with Docker (everything bundled, one URL):

```bash
docker build -t edusync-ai .
docker run --rm -p 7860:7860 --env-file .env edusync-ai     # http://localhost:7860
```

Without `GEMINI_API_KEY` the video feature still works through the manual timeline (Whisper and FFmpeg are local); automatic slide matching and all of answer evaluation need the key.

## Choosing the model: Gemini, OpenRouter or local Ollama

All three are supported (`LLM_PROVIDER` in `.env`):

| Value | Behaviour |
|---|---|
| `gemini` (default) | Gemini free tier (`GEMINI_MODEL` is a fallback list). Most accurate, small daily quota. |
| `openrouter` | Free vision models on OpenRouter (NVIDIA Nemotron VL, then Gemma). Free key, no card. |
| `ollama` | Local model, no key, no quota. Install Ollama, then `ollama pull qwen2.5vl:7b`. |
| `auto` | Gemini first; if it fails, OpenRouter (when `OPENROUTER_API_KEY` is set); last, Ollama. |

A deployed server can reach a laptop's Ollama through a tunnel (`cloudflared tunnel --http-host-header localhost:11434 --url http://localhost:11434`, then set `OLLAMA_URL`). That fallback only works while the laptop is on.

`GRADER_B_PROVIDER=ollama` makes the second grader a different model than the first. Local models are weaker at messy handwriting than Gemini, so expect more "needs review" flags.

## Tests

```bash
cd backend && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q
cd frontend && pnpm typecheck && pnpm build
```

`backend/tests` runs real FFmpeg and Whisper on `samples/`. Grading tests mock the model: they prove the rubric enforcement (clamping, invented criteria, evidence checks, review flags), not OCR quality.

## Records, review and history

Every evaluation and video job is a stored record, not a temporary job:

- Evaluations keep the rubric, the student name, the answer sheet images, the transcript, both graders' marks, every flag and the time. A teacher can confirm or override any criterion with a reason; the AI mark is kept beside the teacher mark (audit trail) and totals use the teacher mark. Export one evaluation or all of them as CSV (cells that start like a formula are neutralised).
- Video jobs keep the uploaded video, the deck, the slide images, the transcript, the timeline and the rendered MP4. Open, re-render, download or delete from the history list.
- A job running when the server restarts is marked "interrupted" and can be retried from the stored inputs.
- Work in progress (rubric text, student name, picked files) is kept when you switch tabs; text also survives a reload.

Storage: set `DATABASE_URL` to a Postgres database (Neon) and both records and files (answer sheet images, slides, rendered videos) are kept there. Without it, SQLite and files go under `DATA_DIR` (default `./data`): fine locally, wiped on every Render restart. Supabase Storage (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`) can hold the files instead. Records are open to anyone with the site URL (no login), so do not upload real student data to a public deployment.

### Neon setup (free, no card)

1. Sign up at https://neon.tech and create a project (pick the region closest to your Render service).
2. Click **Connect** and copy the connection string (`postgresql://...neon.tech/neondb?sslmode=require...`).
3. Set it as `DATABASE_URL` in Render (and in `.env` for local use), then redeploy. Tables are created on start. `/api/health` then reports `"database":"postgres","storage":"database"`.

Neon's free plan has about 0.5 GB of storage. Each stored file is capped at `BLOB_MAX_MB` (default 50); delete old videos from the history list to free space.

## Architecture

```
React SPA ──/api──> FastAPI ── store.py: records + files in Postgres (Neon) or SQLite + local folder
                      ├─ heavy pool (1 worker): LibreOffice -> PyMuPDF PNGs, Whisper, FFmpeg
                      └─ light pool (4):        model calls (OCR, grading): Gemini -> OpenRouter -> Ollama
```

Video: slides to PNG, local Whisper transcript, Gemini picks the transcript sentence where each slide begins (so every cut lands on a sentence boundary, never mid-word), then `clean_timeline` enforces order, minimum dwell and valid slides. If confidence is low, slides are unmatched, speech is missing or there is no key, the editable timeline opens instead (with a draft that is labelled as unmatched). FFmpeg renders from the timeline: auto-detected key colour from the frame corners, `chromakey` + `despill` over white, `xfade` slide track, original audio mapped through (AAC), constant 30 fps.

Grading (two independent graders): Gemini transcribes the pages (equations as text, diagrams as bracketed descriptions, `[illegible]` markers, per-page confidence), then grader A grades each rubric criterion from the transcript plus images, while grader B grades from the page images alone (no transcript, so an OCR misreading cannot silently pass through both). Where they differ, the lower mark is applied and the criterion goes to a human. If grader B is unavailable, the whole submission is flagged. `grading.validate` is the authority: marks clamped to the criterion maximum, criteria not in the rubric are dropped, missing ones score 0, marks without a quote found in the transcript are flagged and capped in confidence, low page or criterion confidence flags the submission for human review.

## Accuracy check

`backend/eval/run_eval.py` grades the five sample sheets and compares each criterion with expected marks (needs a key). A first single-grader run agreed on 33 of 35 criteria; the two misses were a word-only equation given 0.5 of 1 and a missing-formula criterion given 1 instead of 0. The harness has not been re-run on the two-grader version yet (free daily quota was used up while building), and its expected marks were written after that first run, so treat it as a regression check, not an unbiased accuracy estimate.

## Free hosting evaluation

| Host | Backend | FFmpeg | CPU / RAM | Long jobs | Verdict |
|---|---|---|---|---|---|
| Hugging Face Spaces (Docker) | yes | yes (apt) | 2 vCPU / 16 GB | | Docker Spaces now need a paid plan for new accounts. |
| Render free + Neon | yes | via Docker | 0.1 CPU / 512 MB | sleeps after 15 min idle; slow for video, may run out of memory on long clips | **Used.** No card. Records and files in Neon survive restarts. |
| Fly.io, Railway | yes | yes | small | require a card | Not free without a card |
| Vercel / Netlify | serverless only | no | 10 to 60 s limits | no | No (frontend only) |
| Google Cloud Run | yes | yes | ok | free tier needs a billing account | Needs a card |

Deployed on Render's free tier from this repository's Dockerfile. Configure Neon (see above) so records and files survive Render restarts. Render free has a tenth of a CPU: handwriting grading is fine, video processing of long clips is slow and can run out of memory; keep demo clips short.

## Known limitations

- Sample presenter video and handwriting are synthetic (TTS narrator, handwriting-style font). Real footage and real scans are not yet tested.
- Chroma key suits an evenly lit green background; uneven lighting or green clothing will leak. Use the Green screen selector to force on or off.
- Single render worker, so concurrent video users queue. Neon free storage is about 0.5 GB (check your plan's current limits), and files over `BLOB_MAX_MB` stay on the server disk only, with a warning on the job.
- No login: anyone with the URL sees all records.
- Gemini free tier has small daily quotas per model (an evaluation costs 3 calls, a video match 1). The client falls back across `GEMINI_MODEL`'s comma-separated list and gives a clear message when all are used up.
- Gemini free tier has rate limits and may use inputs for model improvement. Do not upload real student data to a free key.
- Slides that are only images have no text for matching; the speech still is used but accuracy drops.

## Recruiter demo

See [DEMO.md](DEMO.md). Sample inputs are in `samples/` (regenerate with `backend/.venv/bin/python samples/make_samples.py`).
