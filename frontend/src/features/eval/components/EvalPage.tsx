import { useNavigate, useSearch } from "@tanstack/react-router"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ErrorNote, StatusCard, WarnNote } from "@/components/StatusCard"
import { useDrafts } from "@/app/drafts"
import { useCreateEvalJob, useEvalJob, useRetryEval } from "../hooks"
import { currentRubric, RubricEditor } from "./RubricEditor"
import { EvalResultView } from "./EvalResultView"
import { EvalHistory } from "./EvalHistory"

export function EvalPage() {
  const { job } = useSearch({ from: "/eval" })
  const navigate = useNavigate({ from: "/eval" })
  const d = useDrafts()
  const create = useCreateEvalJob()
  const retry = useRetryEval()
  const q = useEvalJob(job)
  const s = q.data

  const open = (id: string | undefined) => {
    d.set({ lastEvalJob: id })
    return navigate({ search: { job: id } })
  }
  const parsed = currentRubric(d.rubricMode, d.rubricText, d.rubric)
  const submit = () =>
    parsed.rubric &&
    create.mutate(
      { rubric: JSON.stringify(parsed.rubric), pages: d.pages, student: d.student, title: d.title },
      {
        onSuccess: (r) => {
          d.setPages([]) // the rubric stays for the next student; the sheet and name are per student
          d.set({ student: "" })
          return open(r.id)
        },
      },
    )

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Grade handwritten answers against a rubric</CardTitle>
          <CardDescription>
            The rubric is the only source of truth. Marks are enforced in code (never above the rubric maximum), uncertain readings are flagged, and a teacher can review and override any mark. Every evaluation is stored.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <RubricEditor parsed={parsed} />
            <div className="space-y-3">
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1">
                  <Label htmlFor="stu">Student name or roll number</Label>
                  <Input id="stu" maxLength={200} value={d.student} onChange={(e) => d.set({ student: e.target.value })} placeholder="Optional" />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="ttl">Title</Label>
                  <Input id="ttl" maxLength={200} value={d.title} onChange={(e) => d.set({ title: e.target.value })} placeholder="Defaults to the rubric title" />
                </div>
              </div>
              <div className="space-y-2">
                <Label htmlFor="pg">Answer sheet pages (PNG, JPG, WebP; in order; up to 12)</Label>
                <input id="pg" type="file" accept="image/png,image/jpeg,image/webp" multiple className="block w-full rounded-lg border bg-background p-2 text-sm"
                  onChange={(e) => d.setPages([...(e.target.files ?? [])])} />
                {d.pages.length > 0 && (
                  <div className="flex flex-wrap items-center gap-2">
                    {d.pagePreviews.map((u, i) => <img key={u} src={u} alt={`Page ${i + 1}: ${d.pages[i]?.name ?? ""}`} className="h-20 rounded border" />)}
                    <Button variant="ghost" size="sm" onClick={() => d.setPages([])}>Clear pages</Button>
                  </div>
                )}
              </div>
            </div>
          </div>
          <Button onClick={submit} disabled={!parsed.rubric || d.pages.length === 0 || d.pages.length > 12 || create.isPending}>
            {create.isPending ? "Uploading..." : "Evaluate"}
          </Button>
          {create.error && <ErrorNote message={create.error.message} />}
        </CardContent>
      </Card>

      {q.error && <ErrorNote message={q.error.message} />}
      {s && (s.error || s.stage !== "done") && (
        <StatusCard message={s.message} progress={s.progress} error={s.error} interrupted={s.stage === "interrupted"}
          onRetry={() => retry.mutate(s.id)} retrying={retry.isPending} />
      )}
      {retry.error && <ErrorNote message={retry.error.message} />}
      {s?.storage_warning && <WarnNote message={s.storage_warning} />}
      {s?.result && <EvalResultView key={s.id} job={s} result={s.result} />}

      <EvalHistory current={job} onOpen={open} />
    </div>
  )
}
