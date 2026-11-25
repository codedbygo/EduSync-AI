import { describe, expect, it } from "vitest"
import { parseRubricText, rubricToText, RUBRIC_SAMPLE_FILE, RUBRIC_TEXT_EXAMPLE } from "./rubricText"

describe("parseRubricText", () => {
  it("reads the example a teacher would write", () => {
    const p = parseRubricText(RUBRIC_TEXT_EXAMPLE)
    expect(p.errors).toEqual([])
    expect(p.warnings).toEqual([])
    expect(p.rubric?.title).toBe("Photosynthesis quiz")
    expect(p.rubric?.questions.map((q) => [q.id, q.criteria.length])).toEqual([["Q1", 4], ["Q2", 3]])
    expect(p.rubric?.questions[1].reference_answer).toBe("150 / 2.5 = 60 km/h")
    expect(p.total).toBe(7)
  })

  it("accepts the common ways of writing marks and numbering", () => {
    const p = parseRubricText(`1. Define force.
* Says it is a push or pull (1 mark)
• Gives the unit newton - 0.5 marks
- 2 marks: Uses F = ma
- Mentions direction 1.5 pts
Question 2) Explain inertia
over two lines.
- Defines inertia [2]`)
    expect(p.errors).toEqual([])
    const [q1, q2] = p.rubric!.questions
    expect(q1.criteria.map((c) => [c.description, c.marks])).toEqual([
      ["Says it is a push or pull", 1], ["Gives the unit newton", 0.5], ["Uses F = ma", 2], ["Mentions direction", 1.5],
    ])
    expect(q2.text).toBe("Explain inertia over two lines.")
  })

  it("reads lettered marking points", () => {
    const p = parseRubricText("Q1: Explain.\na) First point [1]\n(b) Second point (2 marks)")
    expect(p.rubric?.questions[0].criteria.map((c) => c.marks)).toEqual([1, 2])
  })

  it("explains every problem by line instead of guessing", () => {
    const p = parseRubricText(`- Orphan point [1]
Q1: Explain.
- Has no marks
Q1: Again.`)
    expect(p.rubric).toBeNull()
    expect(p.errors.join("\n")).toMatch(/Line 1: a marking point must come after a question/)
    expect(p.errors.join("\n")).toMatch(/Line 3: no marks found/)
    expect(p.errors.join("\n")).toMatch(/Line 4: question Q1 appears twice/)
  })

  it("warns when the declared question total disagrees with its marking points", () => {
    const p = parseRubricText("Q1: Explain. (10 marks)\n- Point [2]\n- Point two [3]")
    expect(p.errors).toEqual([])
    expect(p.warnings[0]).toMatch(/Q1 says 10 marks, but its marking points add up to 5/)
  })

  it("round-trips JSON rubrics through text", () => {
    const json = { title: "T", questions: [{ id: "Q1", text: "Why?", reference_answer: "Because.", criteria: [{ id: "a", description: "Gives a reason", marks: 1.5 }] }] }
    const back = parseRubricText(rubricToText(json)).rubric
    expect(back).toEqual({ title: "T", questions: [{ id: "Q1", text: "Why?", reference_answer: "Because.", criteria: [{ id: "c1", description: "Gives a reason", marks: 1.5 }] }] })
  })
})

describe("RUBRIC_SAMPLE_FILE", () => {
  it("uploads as it is: the instructions are ignored and the example parses", () => {
    const p = parseRubricText(RUBRIC_SAMPLE_FILE)
    expect(p.errors).toEqual([])
    expect(p.total).toBe(7)
  })
})
