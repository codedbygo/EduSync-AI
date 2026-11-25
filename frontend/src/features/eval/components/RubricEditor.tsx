import { Button, buttonVariants } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { ErrorNote } from "@/components/StatusCard"
import { useDrafts } from "@/app/drafts"
import { useState } from "react"
import { fetchRubricFileText } from "../api"
import { useSampleRubric } from "../hooks"
import { parseRubricText, rubricToText, RUBRIC_SAMPLE_FILE, RUBRIC_TEXT_EXAMPLE, type Parsed, type Rubric } from "../rubricText"

/** The rubric the form will submit, from whichever editor is active. Pure: computed during render. */
export function currentRubric(mode: "text" | "json", text: string, json: string): Parsed {
  if (mode === "text") return parseRubricText(text)
  if (!json.trim()) return { rubric: null, errors: ["Paste or upload a rubric."], warnings: [], total: 0 }
  try {
    const r = JSON.parse(json) as Rubric // submitted exactly as pasted (custom ids kept); the server validates it in full
    if (!r || !Array.isArray(r.questions) || !r.questions.length) return { rubric: null, errors: ['The JSON needs a non-empty "questions" list.'], warnings: [], total: 0 }
    const total = r.questions.reduce((a, q) => a + (q.criteria ?? []).reduce((b, c) => b + (Number(c.marks) || 0), 0), 0)
    return { rubric: r, errors: [], warnings: [], total }
  } catch (e) {
    return { rubric: null, errors: [`Not valid JSON: ${(e as Error).message}`], warnings: [], total: 0 }
  }
}

const toggle = "rounded-md px-3 py-1 text-sm font-medium aria-pressed:bg-background aria-pressed:shadow-sm text-muted-foreground aria-pressed:text-foreground"

export function RubricEditor({ parsed }: { parsed: Parsed }) {
  const d = useDrafts()
  const sample = useSampleRubric()

  const [upload, setUpload] = useState<{ busy?: boolean; error?: string; name?: string }>({})
  const loadFile = async (f?: File) => {
    if (!f) return
    setUpload({ busy: true })
    try {
      if (f.name.toLowerCase().endsWith(".json")) {
        const body = await f.text()
        const json = JSON.parse(body) as Rubric // a JSON rubric file: show it as text too
        d.set({ rubric: body, rubricText: rubricToText(json), rubricMode: "text" })
      } else {
        d.set({ rubricText: await fetchRubricFileText(f), rubricMode: "text" }) // Word, PDF, text: the server extracts the text
      }
      setUpload({ name: f.name })
    } catch (e) {
      setUpload({ error: e instanceof SyntaxError ? `Not valid JSON: ${e.message}` : (e as Error).message })
    }
  }
  const sampleHref = `data:text/plain;charset=utf-8,${encodeURIComponent(RUBRIC_SAMPLE_FILE)}`
  const switchTo = (mode: "text" | "json") => {
    // carry the current rubric across so nothing typed is lost
    if (mode === "json" && parsed.rubric) d.set({ rubricMode: mode, rubric: JSON.stringify(parsed.rubric, null, 2) })
    else if (mode === "text" && parsed.rubric) d.set({ rubricMode: mode, rubricText: rubricToText(parsed.rubric) })
    else d.set({ rubricMode: mode })
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Label htmlFor="rub">Questions and marking rules</Label>
        <div role="group" aria-label="Rubric format" className="flex gap-1 rounded-lg bg-muted p-1">
          <button type="button" className={toggle} aria-pressed={d.rubricMode === "text"} onClick={() => switchTo("text")}>Simple text</button>
          <button type="button" className={toggle} aria-pressed={d.rubricMode === "json"} onClick={() => switchTo("json")}>JSON (advanced)</button>
        </div>
      </div>

      {d.rubricMode === "text" ? (
        <>
          <p className="text-xs text-muted-foreground">
            One question per line starting with <code>Q1:</code>, then its marking points starting with <code>-</code> and ending with the marks, like <code>[2]</code>. Optional: <code>Title:</code> on the first line, <code>Model answer:</code> under a question.
          </p>
          <Textarea id="rub" spellCheck={false} className="min-h-56 font-mono text-xs" placeholder={RUBRIC_TEXT_EXAMPLE}
            value={d.rubricText} onChange={(e) => d.set({ rubricText: e.target.value })} />
        </>
      ) : (
        <Textarea id="rub" spellCheck={false} className="min-h-56 font-mono text-xs" value={d.rubric} onChange={(e) => d.set({ rubric: e.target.value })} />
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm"
          onClick={() => sample.mutate(undefined, { onSuccess: (r) => d.set({ rubric: JSON.stringify(r, null, 2), rubricText: rubricToText(r) }) })}>
          Load sample rubric
        </Button>
        <a href={sampleHref} download="rubric-sample.txt" className={buttonVariants({ variant: "outline", size: "sm" })}>Download sample rubric file</a>
      </div>
      <div className="space-y-1">
        <Label htmlFor="rubfile">Or upload a rubric file (Word .docx, PDF, .txt or .json), written like the sample</Label>
        <input id="rubfile" type="file" className="block w-full rounded-lg border bg-background p-2 text-sm"
          onChange={(e) => { void loadFile(e.target.files?.[0]); e.target.value = "" }} />
        <p role="status" className="text-xs text-muted-foreground">
          {upload.busy ? "Reading the file..." : upload.name ? `Loaded ${upload.name}. Check the preview below and fix any line it points to.` : ""}
        </p>
      </div>
      {upload.error && <ErrorNote message={upload.error} />}
      {sample.error && <ErrorNote message={sample.error.message} />}

      <RubricPreview parsed={parsed} />
    </div>
  )
}

function RubricPreview({ parsed }: { parsed: Parsed }) {
  const r = parsed.rubric
  return (
    <div className="space-y-2 rounded-lg border bg-muted/30 p-3" aria-live="polite">
      {parsed.errors.length > 0 && (
        <ul className="list-disc space-y-1 pl-5 text-sm text-destructive">{parsed.errors.slice(0, 8).map((e) => <li key={e}>{e}</li>)}</ul>
      )}
      {parsed.warnings.map((w) => <p key={w} className="text-sm text-muted-foreground">Note: {w}</p>)}
      {r && (
        <>
          <div className="flex justify-between text-sm font-semibold">
            <span>{r.title || "Untitled rubric"}: how it will be graded</span>
            <span>Total {parsed.total} marks</span>
          </div>
          {r.questions.map((q) => (
            <div key={q.id} className="text-sm">
              <div className="font-medium">{q.id}: {q.text}</div>
              <ul className="ml-4 list-disc">
                {q.criteria.map((c) => (
                  <li key={c.id} className="flex justify-between gap-3"><span>{c.description}</span><span className="whitespace-nowrap">{c.marks} mark{c.marks === 1 ? "" : "s"}</span></li>
                ))}
              </ul>
              {q.reference_answer && <div className="ml-4 text-xs text-muted-foreground">Model answer: {q.reference_answer}</div>}
            </div>
          ))}
        </>
      )}
    </div>
  )
}
