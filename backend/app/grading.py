"""Handwritten answer evaluation: multimodal OCR -> rubric grading -> code-enforced validation.

The LLM proposes marks; `validate()` is the authority. The rubric is the only source of
truth: criteria the model invents are dropped, missing ones score 0, marks are clamped.
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import llm

LOW_CONF = 0.6  # below this, a criterion or page goes to human review


class RubricError(ValueError):
    pass


def validate_rubric(rubric: dict) -> list[dict]:
    qs = rubric.get("questions") if isinstance(rubric, dict) else None
    if not qs or not isinstance(qs, list):
        raise RubricError("Rubric needs a non-empty 'questions' list.")
    seen_q = set()
    for q in qs:
        qid = str(q.get("id", "")).strip()
        if not qid or qid in seen_q:
            raise RubricError(f"Question id missing or duplicated: '{qid}'.")
        seen_q.add(qid)
        if not str(q.get("text", "")).strip():
            raise RubricError(f"Question {qid} has no text.")
        crits = q.get("criteria")
        if not crits or not isinstance(crits, list):
            raise RubricError(f"Question {qid} has no criteria.")
        seen_c = set()
        for c in crits:
            cid = str(c.get("id", "")).strip()
            if not cid or cid in seen_c:
                raise RubricError(f"Question {qid}: criterion id missing or duplicated: '{cid}'.")
            seen_c.add(cid)
            if not str(c.get("description", "")).strip():
                raise RubricError(f"Question {qid}/{cid}: description is empty.")
            try:
                m = float(c.get("marks"))
            except (TypeError, ValueError):
                raise RubricError(f"Question {qid}/{cid}: marks must be a number.")
            if m <= 0:
                raise RubricError(f"Question {qid}/{cid}: marks must be positive.")
            c["marks"] = m
            c["id"] = cid
        q["id"] = qid
    return qs


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s=+\-*/^().,]", "", s.lower())).strip()


def evidence_found(quote: str, transcript: str) -> bool:
    q = _norm(quote)
    return bool(q) and q in _norm(transcript)


def _marks(r: dict | None, cap: float) -> float | None:
    if r is None:
        return None
    try:
        v = float(r.get("marks_awarded", 0))
    except (TypeError, ValueError):
        return None
    return min(max(v, 0.0), cap) if v == v else None


def validate(rubric_qs: list[dict], raw: dict, transcript: str, page_confidence: float, raw_b: dict | None = None) -> dict:
    """Turn raw model grading into a trusted result. Pure function (unit tested).

    raw_b is an independent second grader (sees the images, not the transcript). Where the two
    graders differ, the LOWER mark is applied and the criterion goes to a human: the student is
    never given marks that only one of two readers believes in without a person looking.
    """
    raw_b_by_q = {str(r.get("question_id")): r for r in (raw_b or {}).get("questions", []) if isinstance(r, dict)}
    raw_by_q = {str(r.get("question_id")): r for r in raw.get("questions", []) if isinstance(r, dict)}
    out_qs, total, max_total, any_review = [], 0.0, 0.0, False
    for q in rubric_qs:
        raw_q = raw_by_q.get(q["id"], {})
        raw_c = {str(c.get("criterion_id")): c for c in raw_q.get("criteria", []) if isinstance(c, dict)}
        raw_bc = {str(c.get("criterion_id")): c for c in raw_b_by_q.get(q["id"], {}).get("criteria", []) if isinstance(c, dict)}
        crits, q_score, q_max = [], 0.0, 0.0
        for c in q["criteria"]:
            r = raw_c.get(c["id"])
            flags, review = [], False
            if r is None:
                r, review = {}, True
                flags.append("model did not grade this criterion; scored 0")
            try:
                awarded = float(r.get("marks_awarded", 0))
            except (TypeError, ValueError):
                awarded = 0.0
                flags.append("unreadable marks from model; scored 0")
            if awarded != awarded:  # NaN
                awarded = 0.0
            clamped = min(max(awarded, 0.0), c["marks"])
            if clamped != awarded:
                flags.append(f"model proposed {awarded:g}, clamped to {clamped:g} (rubric max {c['marks']:g})")
            try:
                conf = min(max(float(r.get("confidence", 0.0)), 0.0), 1.0)
            except (TypeError, ValueError):
                conf = 0.0
            quote = str(r.get("evidence_quote") or "").strip()
            verified = evidence_found(quote, transcript) if quote else False
            if clamped > 0 and not verified:
                flags.append("marks awarded without a quote that can be found in the transcribed answer")
                conf, review = min(conf, 0.4), True
            second = None
            if raw_b is not None:
                second = _marks(raw_bc.get(c["id"]), c["marks"])
                if second is None:
                    flags.append("second grader did not grade this criterion")
                    conf, review = min(conf, 0.5), True
                elif abs(second - clamped) > 1e-9:
                    flags.append(f"two independent graders disagree ({clamped:g} vs {second:g}); lower mark applied")
                    clamped, conf, review = min(clamped, second), min(conf, 0.5), True
            if page_confidence < LOW_CONF:
                flags.append("handwriting read with low confidence")
                conf = min(conf, page_confidence)
            review = review or conf < LOW_CONF
            any_review |= review
            q_score += clamped
            q_max += c["marks"]
            crits.append({
                "criterion_id": c["id"], "description": c["description"],
                "max_marks": c["marks"], "marks_awarded": round(clamped, 2),
                "reasoning": str(r.get("reasoning", "")), "evidence_quote": quote,
                "evidence_verified": verified, "confidence": round(conf, 2),
                "needs_review": review, "flags": flags,
                "second_grader_marks": second, "graders_agree": None if second is None else abs(second - clamped) < 1e-9 and not any("disagree" in f for f in flags),
            })
        total += q_score
        max_total += q_max
        out_qs.append({
            "question_id": q["id"], "question": q["text"], "score": round(q_score, 2), "max_marks": q_max,
            "criteria": crits,
            "missing_concepts": [str(x) for x in raw_q.get("missing_concepts", [])][:20],
            "incorrect_statements": [str(x) for x in raw_q.get("incorrect_statements", [])][:20],
        })
    assert total <= max_total + 1e-9
    return {
        "total_score": round(total, 2), "max_total": max_total, "questions": out_qs,
        "needs_review": any_review or page_confidence < LOW_CONF,
        "ocr_confidence": round(page_confidence, 2),
        "second_opinion": raw_b is not None,
    }


OCR_PROMPT = """You are transcribing handwritten student answer sheets. Images are pages in order.
Transcribe faithfully, do not correct the student's mistakes and do not guess unreadable words.
- Write equations in plain text/LaTeX exactly as written.
- Mark unreadable text as [illegible].
- Describe diagrams, graphs and labels in square brackets, e.g. [DIAGRAM: ...labels: ...].
- Keep question numbers/labels the student wrote.
Return JSON: {"pages":[{"page":1,"text":"...","confidence":0.0-1.0}]}
confidence = how sure you are that the transcription of that page is accurate (low for messy handwriting)."""


def build_grade_prompt(rubric_qs: list[dict], transcript: str | None) -> str:
    rub = []
    for q in rubric_qs:
        rub.append(f"Question {q['id']}: {q['text']}")
        if q.get("reference_answer"):
            rub.append(f"  Reference answer (guidance only): {q['reference_answer']}")
        for c in q["criteria"]:
            rub.append(f"  - criterion_id={c['id']} (max {c['marks']:g} marks): {c['description']}")
    return f"""You are a strict, fair examiner. The RUBRIC is the only source of truth.
Grade each criterion independently by MEANING, not keywords. Accept alternative wording, synonyms,
equivalent equations/forms and correct alternative methods when they satisfy the criterion.
Rules:
- Use only the listed criteria. Never add criteria. Never exceed a criterion's max marks.
- Partial marks are allowed in steps of 0.5 when the criterion is partly met.
- evidence_quote MUST be copied verbatim from the student's writing (empty string if the criterion is not addressed).
- Do not give marks for text marked [illegible]; lower confidence instead.
- confidence (0-1): how sure you are about the marks for that criterion.
- List missing_concepts (rubric ideas absent) and incorrect_statements (wrong claims, quoted) per question.
If the student's answer for a question is absent, give 0 with an empty quote.

RUBRIC:
{chr(10).join(rub)}

{"TRANSCRIBED ANSWER SHEET:" + chr(10) + transcript if transcript is not None else "The student's answer sheet is in the attached images. Read the handwriting yourself."}

Return JSON: {{"questions":[{{"question_id":"..","criteria":[{{"criterion_id":"..","marks_awarded":0,"reasoning":"..","evidence_quote":"..","confidence":0.0}}],"missing_concepts":[".."],"incorrect_statements":[".."]}}]}}"""


def _b_kw() -> dict:
    p = os.getenv("GRADER_B_PROVIDER")
    return {"provider": p} if p else {}


def evaluate(rubric: dict, pages: list[tuple[bytes, str]], progress: Callable[[float, str], None] = lambda p, m: None) -> dict:
    rubric_qs = validate_rubric(rubric)
    progress(0.1, "Reading handwriting")
    ocr = llm.generate_json(OCR_PROMPT, images=pages)
    opages = [p for p in ocr.get("pages", []) if isinstance(p, dict)]
    if not opages:
        raise llm.LLMError("OCR returned no pages")
    transcript = "\n\n".join(f"--- Page {p.get('page', i + 1)} ---\n{p.get('text', '')}" for i, p in enumerate(opages))
    confs = []
    for p in opages:
        try:
            confs.append(min(max(float(p.get("confidence", 0)), 0.0), 1.0))
        except (TypeError, ValueError):
            confs.append(0.0)
    page_conf = min(confs)  # weakest page decides: be conservative
    progress(0.5, "Grading with two independent graders")
    with ThreadPoolExecutor(2) as ex:
        fa = ex.submit(llm.generate_json, build_grade_prompt(rubric_qs, transcript), pages)
        # GRADER_B_PROVIDER=ollama makes grader B a different model family: errors that coincide are rarer
        fb = ex.submit(lambda: llm.generate_json(build_grade_prompt(rubric_qs, None), pages, **_b_kw()))
        raw = fa.result()  # grader A failing is fatal
        try:
            raw_b = fb.result()
        except llm.LLMError:
            raw_b = None  # no second opinion: every mark goes to review (see below)
    result = validate(rubric_qs, raw, transcript, page_conf, raw_b)
    if raw_b is None:
        result["needs_review"] = True
    result["transcript"] = transcript
    progress(1.0, "Done")
    return result
