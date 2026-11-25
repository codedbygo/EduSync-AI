"""Tests of the code-enforced layer. The model is mocked: these prove rubric enforcement, not OCR quality."""
import copy, json
import pytest
from app import grading, llm

RUB = json.load(open(__file__.replace("backend/tests/test_grading.py", "backend/app/sample-rubric.json")))
TEXT = "Q1. Inputs are carbon dioxide and water. Chlorophyll in the chloroplasts absorbs light energy. It produces glucose and oxygen. 6CO2 + 6H2O -> C6H12O6 + 6O2\nQ2. speed = distance / time = 150 / 2.5 = 60 km/h"


def raw(marks, quote=TEXT[:30], conf=0.9, only=None):
    qs = []
    for q in RUB["questions"]:
        crits = [{"criterion_id": c["id"], "marks_awarded": marks.get(c["id"], 0), "reasoning": "r", "evidence_quote": quote, "confidence": conf}
                 for c in q["criteria"] if only is None or c["id"] in only]
        qs.append({"question_id": q["id"], "criteria": crits})
    return {"questions": qs}


def run(r, text=TEXT, page=0.95):
    return grading.validate(grading.validate_rubric(copy.deepcopy(RUB)), r, text, page)


def test_full_marks():
    r = run(raw({k: 1 for k in ["inputs", "outputs", "chlorophyll", "equation", "formula", "substitution", "answer"]}))
    assert r["total_score"] == r["max_total"] == 7 and not r["needs_review"]


def test_clamps_over_max_and_negative():
    r = run(raw({"inputs": 5, "outputs": -2}))
    c = {c["criterion_id"]: c for q in r["questions"] for c in q["criteria"]}
    assert c["inputs"]["marks_awarded"] == 1 and c["outputs"]["marks_awarded"] == 0
    assert r["total_score"] <= r["max_total"]
    assert any("clamped" in f for f in c["inputs"]["flags"])


def test_invented_criteria_dropped_missing_scored_zero():
    x = raw({"inputs": 1}, only={"inputs"})
    x["questions"][0]["criteria"].append({"criterion_id": "bonus", "marks_awarded": 10, "evidence_quote": "", "confidence": 1})
    r = run(x)
    ids = [c["criterion_id"] for c in r["questions"][0]["criteria"]]
    assert "bonus" not in ids and ids == ["inputs", "outputs", "chlorophyll", "equation"]
    assert r["questions"][0]["criteria"][1]["needs_review"]


def test_marks_without_verifiable_quote_flagged():
    r = run(raw({"inputs": 1}, quote="the student wrote something that is not there"))
    c = r["questions"][0]["criteria"][0]
    assert c["evidence_verified"] is False and c["needs_review"] and r["needs_review"]


def test_low_page_confidence_forces_review():
    r = run(raw({"inputs": 1}), page=0.3)
    assert r["needs_review"] and all(c["needs_review"] for q in r["questions"] for c in q["criteria"])


def test_garbage_model_output_never_crashes():
    r = run({"questions": [{"question_id": "Q1", "criteria": [{"criterion_id": "inputs", "marks_awarded": "lots", "confidence": "x"}]}, "junk"]})
    assert r["total_score"] == 0


def test_bad_rubrics_rejected():
    for bad in [{}, {"questions": []}, {"questions": [{"id": "A", "text": "t", "criteria": [{"id": "x", "description": "d", "marks": 0}]}]},
                {"questions": [{"id": "A", "text": "t", "criteria": [{"id": "x", "description": "d", "marks": 1}, {"id": "x", "description": "d", "marks": 1}]}]}]:
        with pytest.raises(grading.RubricError):
            grading.validate_rubric(bad)


def test_evaluate_pipeline_with_mock_model(monkeypatch):
    calls = []
    def fake(prompt, images=None, retries=4):
        calls.append(prompt[:20])
        if "transcribing" in prompt:
            return {"pages": [{"page": 1, "text": TEXT, "confidence": 0.9}]}
        return raw({"inputs": 1, "outputs": 1, "chlorophyll": 1, "equation": 1, "formula": 1, "substitution": 1, "answer": 1})
    monkeypatch.setattr(llm, "generate_json", fake)
    r = grading.evaluate(RUB, [(b"x", "image/png")])
    assert r["total_score"] == 7 and len(calls) == 3 and r["second_opinion"] and not r["needs_review"]


def test_alt_wording_prompt_demands_semantic_grading():
    p = grading.build_grade_prompt(grading.validate_rubric(copy.deepcopy(RUB)), TEXT)
    assert "alternative wording" in p and "Never add criteria" in p


ALL = {k: 1 for k in ["inputs", "outputs", "chlorophyll", "equation", "formula", "substitution", "answer"]}


def test_graders_disagree_lower_mark_and_review():
    b = raw({**ALL, "chlorophyll": 0})
    r = grading.validate(grading.validate_rubric(copy.deepcopy(RUB)), raw(ALL), TEXT, 0.95, b)
    c = {c["criterion_id"]: c for q in r["questions"] for c in q["criteria"]}
    assert c["chlorophyll"]["marks_awarded"] == 0 and c["chlorophyll"]["needs_review"] and c["chlorophyll"]["graders_agree"] is False
    assert c["inputs"]["graders_agree"] is True and r["total_score"] == 6 and r["needs_review"]


def test_graders_agree_no_review():
    r = grading.validate(grading.validate_rubric(copy.deepcopy(RUB)), raw(ALL), TEXT, 0.95, raw(ALL))
    assert r["total_score"] == 7 and not r["needs_review"]


def test_second_grader_never_raises_marks():
    r = grading.validate(grading.validate_rubric(copy.deepcopy(RUB)), raw({"inputs": 0}), TEXT, 0.95, raw({"inputs": 1}))
    assert r["questions"][0]["criteria"][0]["marks_awarded"] == 0


def test_second_grader_failure_forces_review(monkeypatch):
    def fake(prompt, images=None, retries=4):
        if "transcribing" in prompt:
            return {"pages": [{"page": 1, "text": TEXT, "confidence": 0.9}]}
        if "TRANSCRIBED ANSWER SHEET" not in prompt:
            raise llm.LLMError("boom")
        return raw(ALL)
    monkeypatch.setattr(llm, "generate_json", fake)
    r = grading.evaluate(RUB, [(b"x", "image/png")])
    assert r["needs_review"] and r["second_opinion"] is False and r["total_score"] == 7


def test_ollama_provider_request_shape(monkeypatch):
    import io, httpx
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (3000, 2000), "white").save(buf, "PNG")
    seen = {}
    import contextlib
    @contextlib.contextmanager
    def fake_stream(method, url, json=None, timeout=None, **kw):
        seen.update(url=url, body=json)
        class R:
            status_code = 200
            def iter_lines(self):  # two streamed chunks, as Ollama sends them
                yield '{"message": {"content": "{\\"ok\\""}}'
                yield '{"message": {"content": ": 1}"}, "done": true}'
        yield R()
    monkeypatch.setattr(httpx, "stream", fake_stream)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert llm.generate_json("hi", [(buf.getvalue(), "image/png")]) == {"ok": 1}
    assert seen["url"].endswith("/api/chat") and seen["body"]["format"] == "json" and len(seen["body"]["messages"][0]["images"]) == 1
    assert llm.available()


def test_openrouter_skips_failed_model_and_sends_image(monkeypatch):
    import io, httpx
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (2000, 1000), "white").save(buf, "PNG")
    calls = []
    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        calls.append(json["model"])
        class R:
            status_code = 429 if len(calls) == 1 else 200
            text = "rate limited"
            def json(self): return {"choices": [{"message": {"content": '{"ok": 2}'}}]}
        seen_content.append(json["messages"][0]["content"])
        return R()
    seen_content = []
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setenv("OPENROUTER_MODEL", "a:free,b:free")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    assert llm.generate_json("hi", [(buf.getvalue(), "image/png")]) == {"ok": 2}
    assert calls == ["a:free", "b:free"] and seen_content[0][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_auto_order_gemini_then_openrouter_then_ollama(monkeypatch):
    order = []
    def mk(name, fail):
        def f(*a, **k):
            order.append(name)
            if fail: raise llm.LLMError(name)
            return {"from": name}
        return f
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setattr(llm, "_call", lambda *a, **k: (order.append("gemini"), (_ for _ in ()).throw(llm.LLMError("g")))[1])
    monkeypatch.setattr(llm, "_openrouter", mk("openrouter", True))
    monkeypatch.setattr(llm, "_ollama", mk("ollama", False))
    assert llm.generate_json("hi") == {"from": "ollama"} and order == ["gemini", "openrouter", "ollama"]
    order.clear()
    monkeypatch.setattr(llm, "_openrouter", mk("openrouter", False))  # OpenRouter answers: Ollama is never called
    assert llm.generate_json("hi") == {"from": "openrouter"} and order == ["gemini", "openrouter"]
