"""Score the grader against human-decided marks for the sample sheets. Needs GEMINI_API_KEY.
    backend/.venv/bin/python backend/eval/run_eval.py
Expected marks were written from the rubric text and the sheet content, but AFTER an earlier single-grader run had been seen,
so treat the agreement figure as a regression check, not an unbiased accuracy estimate. The sheets are synthetic (handwriting-style font), so this measures the grading logic, not real-world OCR.
"""
import json, os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
for l in (ROOT / ".env").read_text().splitlines():
    if "=" in l and not l.startswith("#") and l.split("=", 1)[1].strip():
        os.environ.setdefault(*[x.strip() for x in l.split("=", 1)])
from app import grading  # noqa: E402

IDS = ["inputs", "outputs", "chlorophyll", "equation", "formula", "substitution", "answer"]
EXPECTED = {  # criterion marks, in IDS order
    "answer_correct":     [1, 1, 1, 1, 1, 1, 1],
    "answer_partial":     [1, 1, 0, 0, 1, 1, 0],
    "answer_incorrect":   [0, 0, 0, 0, 0, 1, 0],   # values 150 and 2.5 are used; formula and answer are wrong
    "answer_alt_wording": [1, 1, 1, 1, 1, 1, 1],   # different words, word equation allowed by the rubric
    "answer_unclear":     [1, 1, 0, 0, 0, 1, 0.5], # blurred page, partial answers only
}
rubric = json.loads((ROOT / "backend/app/sample-rubric.json").read_text())
rows, exact, n, abs_err, over = [], 0, 0, 0.0, 0
for name, exp in EXPECTED.items():
    img = (ROOT / "samples" / f"{name}.png").read_bytes()
    time.sleep(20)  # stay under the free-tier per-minute quota
    r = grading.evaluate(json.loads(json.dumps(rubric)), [(img, "image/png")])
    got = {c["criterion_id"]: c for q in r["questions"] for c in q["criteria"]}
    marks = [got[i]["marks_awarded"] for i in IDS]
    hit = sum(1 for a, b in zip(marks, exp) if abs(a - b) < 1e-9)
    exact += hit; n += len(IDS); abs_err += abs(sum(marks) - sum(exp)); over += r["total_score"] > r["max_total"]
    dis = sum(1 for i in IDS if got[i]["graders_agree"] is False)
    rows.append(f"| {name} | {sum(exp):g} | {r['total_score']:g} | {hit}/{len(IDS)} | {dis} | {'yes' if r['needs_review'] else 'no'} |")
    print(rows[-1], flush=True)
out = ["| sheet | expected total | app total | criteria matching | grader disagreements | flagged for review |", "|---|---|---|---|---|---|", *rows,
       "", f"Criterion-level agreement with expected marks: {exact}/{n} ({exact / n:.0%}). Mean absolute error on totals: {abs_err / len(EXPECTED):.2f} marks. Scores above max: {over}."]
(ROOT / "backend/eval/RESULTS.md").write_text("\n".join(out) + "\n")
print("\n".join(out[-1:]))
