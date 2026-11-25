import { useState } from "react"
import { Button, buttonVariants } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Input } from "@/components/ui/input"
import { ErrorNote, when } from "@/components/StatusCard"
import { useOverride } from "../hooks"
import type { Criterion, EvalJob, EvalResult } from "../schemas"

const tone = (c: number) => (c >= 0.8 ? "secondary" : c >= 0.6 ? "outline" : "destructive")
const pct = (c: number) => `${Math.round(c * 100)}%`

function download(job: EvalJob, r: EvalResult) {
  const a = document.createElement("a")
  a.href = URL.createObjectURL(new Blob([JSON.stringify({ id: job.id, student: job.student, title: job.title, created: job.created, ...r }, null, 2)], { type: "application/json" }))
  a.download = `evaluation_${job.id}.json`
  a.click()
  URL.revokeObjectURL(a.href)
}

function Review({ jobId, qid, c }: { jobId: string; qid: string; c: Criterion }) {
  const [marks, setMarks] = useState(c.teacher_marks != null ? String(c.teacher_marks) : "")
  const [note, setNote] = useState(c.teacher_note)
  const save = useOverride(jobId)
  const n = Number(marks)
  const valid = marks.trim() !== "" && Number.isFinite(n) && n >= 0 && n <= c.max_marks
  const id = `${qid}-${c.criterion_id}`

  return (
    <div className="space-y-2 rounded-md bg-muted/50 p-2">
      <div className="flex flex-wrap items-end gap-2">
        <label className="space-y-1 text-sm" htmlFor={`m-${id}`}>
          <span className="block font-medium">Teacher mark (0 to {c.max_marks})</span>
          <Input id={`m-${id}`} type="number" min={0} max={c.max_marks} step={0.5} className="w-28" value={marks} onChange={(e) => setMarks(e.target.value)} />
        </label>
        <label className="min-w-48 flex-1 space-y-1 text-sm" htmlFor={`n-${id}`}>
          <span className="block font-medium">Reason</span>
          <Input id={`n-${id}`} maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Why the mark changed (kept in the record)" />
        </label>
        <Button size="sm" disabled={!valid || save.isPending} onClick={() => save.mutate({ q: qid, c: c.criterion_id, marks: n, note })}>
          {c.teacher_marks != null ? "Update" : "Confirm mark"}
        </Button>
        {c.teacher_marks != null && (
          <Button size="sm" variant="ghost" disabled={save.isPending} onClick={() => save.mutate({ q: qid, c: c.criterion_id, marks: null, note: "" })}>
            Use AI mark
          </Button>
        )}
      </div>
      {marks.trim() !== "" && !valid && <p className="text-sm text-destructive">Enter a number from 0 to {c.max_marks}.</p>}
      {c.reviewed_at && <p className="text-xs text-muted-foreground">Reviewed {new Date(c.reviewed_at).toLocaleString()}; AI mark was {c.marks_awarded}.</p>}
      {save.error && <ErrorNote message={save.error.message} />}
    </div>
  )
}

export function EvalResultView({ job, result: r }: { job: EvalJob; result: EvalResult }) {
  const changed = r.final_total !== r.total_score
  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="space-y-3 pt-6">
          <div className="text-sm text-muted-foreground">{job.student || "Student not given"} · {job.title} · {when(job.created)}</div>
          <div className="flex flex-wrap items-center gap-4">
            <div className="text-5xl font-extrabold tracking-tight">{r.final_total} / {r.max_total}</div>
            <div className="flex flex-wrap gap-2">
              {changed && <Badge variant="outline">AI proposed {r.total_score}</Badge>}
              {r.open_reviews > 0
                ? <Badge variant="destructive">{r.open_reviews} mark{r.open_reviews === 1 ? "" : "s"} to review</Badge>
                : <Badge variant="secondary">{r.needs_review ? "All flagged marks reviewed" : "No review flags"}</Badge>}
              {r.second_opinion === false && <Badge variant="destructive">No second grader</Badge>}
              <Badge variant={tone(r.ocr_confidence)}>Handwriting confidence {pct(r.ocr_confidence)}</Badge>
            </div>
          </div>
          {r.open_reviews > 0 && (
            <Alert variant="destructive"><AlertDescription>
              Some marks are uncertain (unclear handwriting, unverifiable evidence, graders disagreeing or low confidence). Confirm or correct each flagged criterion below before releasing this grade.
            </AlertDescription></Alert>
          )}
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => download(job, r)}>Download JSON report</Button>
            <a className={buttonVariants({ variant: "outline" })} href={`/api/eval/jobs/${job.id}/export.csv`}>Download CSV</a>
          </div>
        </CardContent>
      </Card>

      {job.page_urls.length > 0 && (
        <Card>
          <CardHeader><CardTitle className="text-base">Answer sheet</CardTitle></CardHeader>
          <CardContent className="flex flex-wrap gap-3">
            {job.page_urls.map((u, i) => (
              <a key={u} href={u} target="_blank" rel="noreferrer" aria-label={`Open page ${i + 1} full size`}>
                <img src={u} alt={`Answer sheet page ${i + 1}`} className="h-48 rounded border" loading="lazy" />
              </a>
            ))}
          </CardContent>
        </Card>
      )}

      {r.questions.map((q) => (
        <Card key={q.question_id}>
          <CardHeader>
            <CardTitle className="flex justify-between gap-4 text-base"><span>{q.question_id}: {q.question}</span><span className="whitespace-nowrap">{q.final_score} / {q.max_marks}</span></CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {q.criteria.map((c) => (
              <div key={c.criterion_id} className="space-y-2 rounded-lg border p-3">
                <div className="flex justify-between gap-3 font-medium">
                  <span>{c.description}</span>
                  <span className="whitespace-nowrap">{c.final_marks} / {c.max_marks}{c.teacher_marks != null && <span className="text-sm font-normal text-muted-foreground"> (AI {c.marks_awarded})</span>}</span>
                </div>
                <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                  <span>{c.reasoning}</span>
                  <Badge variant={tone(c.confidence)}>confidence {pct(c.confidence)}</Badge>
                  {c.graders_agree === true && <Badge variant="secondary">2 graders agree</Badge>}
                  {c.graders_agree === false && <Badge variant="destructive">graders disagree (other: {c.second_grader_marks})</Badge>}
                  {c.needs_review && c.teacher_marks == null && <Badge variant="destructive">review</Badge>}
                  {c.teacher_marks != null && <Badge variant="secondary">teacher reviewed</Badge>}
                </div>
                {c.evidence_quote ? (
                  <blockquote className="whitespace-pre-line border-l-2 border-primary bg-muted/50 px-3 py-1 text-sm italic">
                    {c.evidence_quote}{c.evidence_verified ? "" : " (not found verbatim in transcript)"}
                  </blockquote>
                ) : <div className="text-sm text-muted-foreground">No supporting text found in the answer.</div>}
                {c.flags.map((f) => <div key={f} className="text-sm text-destructive">Warning: {f}</div>)}
                {c.teacher_note && <div className="text-sm"><strong>Teacher note:</strong> <span className="whitespace-pre-line">{c.teacher_note}</span></div>}
                <Review key={c.reviewed_at ?? "ai"} jobId={job.id} qid={q.question_id} c={c} />
              </div>
            ))}
            {q.missing_concepts.length > 0 && <p className="text-sm"><strong>Missing concepts:</strong> {q.missing_concepts.join("; ")}</p>}
            {q.incorrect_statements.length > 0 && <p className="text-sm"><strong>Incorrect statements:</strong> {q.incorrect_statements.join("; ")}</p>}
          </CardContent>
        </Card>
      ))}
      <Card>
        <CardContent className="pt-6"><details><summary className="cursor-pointer font-medium">What the OCR read (transcript)</summary>
          <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-muted p-3 text-xs">{r.transcript}</pre></details></CardContent>
      </Card>
    </div>
  )
}
