import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Alert, AlertDescription } from "@/components/ui/alert"
import type { EvalResult } from "../schemas"

const tone = (c: number) => (c >= 0.8 ? "secondary" : c >= 0.6 ? "outline" : "destructive")
const pct = (c: number) => `${Math.round(c * 100)}%`

function download(r: EvalResult) {
  const a = document.createElement("a")
  a.href = URL.createObjectURL(new Blob([JSON.stringify(r, null, 2)], { type: "application/json" }))
  a.download = "evaluation_report.json"
  a.click()
  URL.revokeObjectURL(a.href)
}

export function EvalResultView({ result: r }: { result: EvalResult }) {
  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="space-y-3 pt-6">
          <div className="flex flex-wrap items-center gap-4">
            <div className="text-5xl font-extrabold tracking-tight">{r.total_score} / {r.max_total}</div>
            <div className="space-x-2">
              <Badge variant={r.needs_review ? "destructive" : "secondary"}>{r.needs_review ? "Needs human review" : "No review flags"}</Badge>
              {r.second_opinion === false && <Badge variant="destructive">No second grader</Badge>}
              <Badge variant={tone(r.ocr_confidence)}>Handwriting confidence {pct(r.ocr_confidence)}</Badge>
            </div>
          </div>
          {r.needs_review && (
            <Alert variant="destructive"><AlertDescription>
              Some marks are uncertain (unclear handwriting, unverifiable evidence or low confidence). Check the flagged criteria before releasing this grade.
            </AlertDescription></Alert>
          )}
          <Button variant="outline" onClick={() => download(r)}>Download JSON report</Button>
        </CardContent>
      </Card>
      {r.questions.map((q) => (
        <Card key={q.question_id}>
          <CardHeader>
            <CardTitle className="flex justify-between gap-4 text-base"><span>{q.question_id}: {q.question}</span><span>{q.score} / {q.max_marks}</span></CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {q.criteria.map((c) => (
              <div key={c.criterion_id} className="space-y-1 rounded-lg border p-3">
                <div className="flex justify-between gap-3 font-medium"><span>{c.description}</span><span>{c.marks_awarded} / {c.max_marks}</span></div>
                <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                  <span>{c.reasoning}</span>
                  <Badge variant={tone(c.confidence)}>confidence {pct(c.confidence)}</Badge>
                  {c.graders_agree === true && <Badge variant="secondary">2 graders agree</Badge>}
                  {c.graders_agree === false && <Badge variant="destructive">graders disagree</Badge>}
                  {c.needs_review && <Badge variant="destructive">review</Badge>}
                </div>
                {c.evidence_quote ? (
                  <blockquote className="whitespace-pre-line border-l-2 border-primary bg-muted/50 px-3 py-1 text-sm italic">
                    {c.evidence_quote}{c.evidence_verified ? "" : " (not found verbatim in transcript)"}
                  </blockquote>
                ) : <div className="text-sm text-muted-foreground">No supporting text found in the answer.</div>}
                {c.flags.map((f) => <div key={f} className="text-sm text-destructive">Warning: {f}</div>)}
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
