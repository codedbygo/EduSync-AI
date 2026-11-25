/** Plain-text rubric <-> rubric JSON. Deterministic rules (no AI), so the rubric is exactly what the teacher wrote.
 *
 *   Title: Photosynthesis quiz
 *   Q1: Explain how photosynthesis works. (4 marks)
 *   - States that carbon dioxide and water are the inputs [1]
 *   - Gives the balanced equation (1.5 marks)
 *   Model answer: 6CO2 + 6H2O -> C6H12O6 + 6O2
 */
export type Criterion = { id: string; description: string; marks: number }
export type Question = { id: string; text: string; reference_answer?: string; criteria: Criterion[] }
export type Rubric = { title?: string; questions: Question[] }
export type Parsed = { rubric: Rubric | null; errors: string[]; warnings: string[]; total: number }

const NUM = String.raw`(\d+(?:\.\d+)?)`
const UNIT = String.raw`(?:marks?|mks?|m|points?|pts?)`
const BULLET = /^\s*(?:[-*•–—]|\(?[a-h][.)](?=\s))\s*/i // "-", "*", "•", or a letter "a)" / "(b)"; numbers like "1." are questions
const QUESTION = /^\s*(?:q(?:uestion)?\s*)?(\d+[a-z]?)\s*[:.)]\s*(.+)$/i
const TITLE = /^\s*title\s*:\s*(.+)$/i
const ANSWER = /^\s*(?:model\s+answer|reference(?:\s+answer)?|answer)\s*:\s*(.+)$/i
const MARK_PATTERNS: RegExp[] = [
  new RegExp(String.raw`^(.*?)\s*[([]\s*${NUM}\s*${UNIT}?\s*[)\]]\s*$`, "i"), // "... [2]" or "... (2 marks)"
  new RegExp(String.raw`^(.*?)\s*[-–—:=]\s*${NUM}\s*${UNIT}\s*$`, "i"), // "... - 2 marks"
  new RegExp(String.raw`^(.*?)\s+${NUM}\s*${UNIT}\s*$`, "i"), // "... 2 marks"
]
const LEADING_MARKS = new RegExp(String.raw`^[([]?\s*${NUM}\s*${UNIT}?\s*[)\]]?\s*[:\-–—]\s*(.+)$`, "i") // "2 marks: ..." / "[2] - ..."
const DECLARED = new RegExp(String.raw`[([]\s*${NUM}\s*${UNIT}\s*[)\]]\s*$`, "i") // "(10 marks)" at the end of a question

function splitMarks(text: string): { description: string; marks: number } | null {
  for (const re of MARK_PATTERNS) {
    const m = text.match(re)
    if (m && m[1].trim()) return { description: m[1].trim(), marks: Number(m[2]) }
  }
  const lead = text.match(LEADING_MARKS)
  if (lead) return { description: lead[2].trim(), marks: Number(lead[1]) }
  return null
}

export function parseRubricText(src: string): Parsed {
  const errors: string[] = []
  const warnings: string[] = []
  let title: string | undefined
  const questions: Question[] = []
  let q: Question | undefined

  src.split(/\r?\n/).forEach((raw, i) => {
    const line = raw.trim()
    const n = i + 1
    if (!line || line.startsWith("#")) return
    const t = line.match(TITLE)
    if (t && !questions.length) {
      title = t[1].trim()
      return
    }
    const ans = line.match(ANSWER)
    if (ans) {
      if (!q) errors.push(`Line ${n}: a model answer must come after its question.`)
      else q.reference_answer = (q.reference_answer ? q.reference_answer + " " : "") + ans[1].trim()
      return
    }
    if (BULLET.test(line)) {
      const body = line.replace(BULLET, "")
      if (!q) {
        errors.push(`Line ${n}: a marking point must come after a question line (for example "Q1: ...").`)
        return
      }
      const cm = splitMarks(body)
      if (!cm) {
        errors.push(`Line ${n}: no marks found in "${body.slice(0, 60)}". End the line with the marks, for example [2].`)
        return
      }
      if (!(cm.marks > 0)) errors.push(`Line ${n}: marks must be more than 0.`)
      q.criteria.push({ id: `c${q.criteria.length + 1}`, description: cm.description, marks: cm.marks })
      return
    }
    const qm = line.match(QUESTION)
    if (qm) {
      const id = `Q${qm[1].toUpperCase()}`
      if (questions.some((x) => x.id === id)) errors.push(`Line ${n}: question ${id} appears twice.`)
      q = { id, text: qm[2].trim(), criteria: [] }
      questions.push(q)
      return
    }
    if (q && q.criteria.length === 0) {
      q.text += " " + line // a question written over several lines
      return
    }
    errors.push(`Line ${n}: not understood. Start questions with "Q1:" and marking points with "-" plus the marks, for example "- Defines the term [1]".`)
  })

  if (!questions.length && !errors.length) errors.push('Add at least one question, for example "Q1: Explain photosynthesis." followed by marking points.')
  for (const x of questions) {
    if (!x.criteria.length) errors.push(`${x.id} has no marking points. Add lines like "- States the inputs [1]".`)
    const declared = x.text.match(DECLARED)
    const sum = x.criteria.reduce((a, c) => a + c.marks, 0)
    if (declared && x.criteria.length && Math.abs(Number(declared[1]) - sum) > 1e-9)
      warnings.push(`${x.id} says ${declared[1]} marks, but its marking points add up to ${sum}. Grading uses the marking points.`)
  }
  const total = questions.reduce((a, x) => a + x.criteria.reduce((b, c) => b + c.marks, 0), 0)
  return { rubric: errors.length ? null : { ...(title ? { title } : {}), questions }, errors, warnings, total }
}

type LooseRubric = { title?: unknown; questions?: { id?: unknown; text?: unknown; reference_answer?: unknown; criteria?: { description?: unknown; marks?: unknown }[] }[] }

/** Rubric JSON (e.g. the sample) -> the plain-text form, so a teacher can edit it without JSON. */
export function rubricToText(r: LooseRubric): string {
  const out: string[] = []
  if (typeof r.title === "string" && r.title) out.push(`Title: ${r.title}`, "")
  ;(r.questions ?? []).forEach((q, i) => {
    const num = String(q.id ?? "").match(/^q?(\d+[a-z]?)$/i)?.[1] ?? String(i + 1)
    out.push(`Q${num}: ${String(q.text ?? "")}`)
    for (const c of q.criteria ?? []) out.push(`- ${String(c.description ?? "")} [${Number(c.marks)}]`)
    if (typeof q.reference_answer === "string" && q.reference_answer) out.push(`Model answer: ${q.reference_answer}`)
    out.push("")
  })
  return out.join("\n").trim() + "\n"
}

/** The downloadable sample: the instructions are "#" lines, which the parser ignores, so the file uploads as it is. */
const SAMPLE_HEADER = `# EduSync AI rubric file. Write your rubric like the example below, save it, and upload it.
# Accepted files: Word (.docx), PDF with text (not a scan), or plain text (.txt).
#
# Rules:
#   Title: <name>                    optional, first line
#   Q1: <question text>              one line per question (also "1." or "Question 1)")
#   - <marking point> [2]            one line per marking point, ending with its marks
#                                    ("(2 marks)" or "- 2 marks" also work)
#   Model answer: <answer>           optional, under its question
#   Lines starting with # are ignored. Empty lines are fine.
#
# Marks are given only for the marking points. The total is the sum of all [marks].

`

export const RUBRIC_TEXT_EXAMPLE = `Title: Photosynthesis quiz

Q1: Explain how photosynthesis converts light into chemical energy. (4 marks)
- States that carbon dioxide and water are the inputs [1]
- States that glucose and oxygen are produced [1]
- Explains that chlorophyll absorbs light energy [1]
- Gives the balanced equation 6CO2 + 6H2O -> C6H12O6 + 6O2 [1]

Q2: A car travels 150 km in 2.5 hours. Find its average speed. (3 marks)
- Uses speed = distance / time [1]
- Substitutes 150 and 2.5 [1]
- Gets 60 km/h with the unit [1]
Model answer: 150 / 2.5 = 60 km/h`

export const RUBRIC_SAMPLE_FILE = SAMPLE_HEADER + RUBRIC_TEXT_EXAMPLE + "\n"
