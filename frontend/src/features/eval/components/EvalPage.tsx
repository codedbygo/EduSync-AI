import { useState } from "react"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { ErrorNote, StatusCard } from "@/components/StatusCard"
import { useCreateEvalJob, useEvalJob, useSampleRubric } from "../hooks"
import { EvalResultView } from "./EvalResultView"

export function EvalPage() {
  const { job } = useSearch({ from: "/eval" })
  const navigate = useNavigate({ from: "/eval" })
  const [rubric, setRubric] = useState("")
  const [pages, setPages] = useState<File[]>([])
  const create = useCreateEvalJob()
  const sample = useSampleRubric()
  const q = useEvalJob(job)
  const s = q.data

  const loadFile = async (f?: File) => f && setRubric(await f.text())
  const submit = () => create.mutate({ rubric, pages }, { onSuccess: (r) => navigate({ search: { job: r.id } }) })

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Grade handwritten answers against a rubric</CardTitle>
          <CardDescription>
            The rubric is the only source of truth. Marks are enforced in code (never above the rubric maximum) and uncertain readings are flagged for a human.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="rub">Questions and marking rubric (JSON)</Label>
              <Textarea id="rub" spellCheck={false} className="min-h-56 font-mono text-xs" value={rubric} onChange={(e) => setRubric(e.target.value)} />
              <div className="flex flex-wrap items-center gap-2">
                <Button variant="outline" size="sm" onClick={() => sample.mutate(undefined, { onSuccess: (r) => setRubric(JSON.stringify(r, null, 2)) })}>
                  Load sample rubric
                </Button>
                <input aria-label="Upload rubric JSON file" type="file" accept=".json" className="text-sm" onChange={(e) => loadFile(e.target.files?.[0])} />
              </div>
              {sample.error && <ErrorNote message={sample.error.message} />}
            </div>
            <div className="space-y-2">
              <Label htmlFor="pg">Answer sheet pages (PNG, JPG, WebP; in order)</Label>
              <input id="pg" type="file" accept="image/*" multiple className="block w-full rounded-lg border bg-background p-2 text-sm"
                onChange={(e) => setPages([...(e.target.files ?? [])])} />
              <div className="flex flex-wrap gap-2">
                {pages.map((f) => <img key={f.name + f.size} src={URL.createObjectURL(f)} alt={f.name} className="h-20 rounded border" />)}
              </div>
            </div>
          </div>
          <Button onClick={submit} disabled={!rubric.trim() || pages.length === 0 || create.isPending}>
            {create.isPending ? "Uploading..." : "Evaluate"}
          </Button>
          {create.error && <ErrorNote message={create.error.message} />}
        </CardContent>
      </Card>
      {q.error && <ErrorNote message={q.error.message} />}
      {s && (s.error || s.stage !== "done") && <StatusCard message={s.message} progress={s.progress} error={s.error} />}
      {s?.result && <EvalResultView result={s.result} />}
    </div>
  )
}
