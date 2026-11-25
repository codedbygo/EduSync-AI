import { useRef, useState } from "react"
import { Plus, X } from "lucide-react"
import { Button, buttonVariants } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { ErrorNote } from "@/components/StatusCard"
import { useRender } from "../hooks"
import type { TimelineEntry, VideoJob } from "../schemas"

const selectCls = "h-9 rounded-lg border bg-background px-2 text-sm"

export function TimelineEditor({ job }: { job: VideoJob }) {
  // Local draft of the timeline: the user edits it, then it is sent to the server to render.
  const [rows, setRows] = useState<TimelineEntry[]>(job.timeline ?? [])
  const [chroma, setChroma] = useState("auto")
  const src = useRef<HTMLVideoElement>(null)
  const render = useRender(job.id)
  const rendering = job.stage === "rendering"
  const n = job.n_slides ?? 0
  const patch = (i: number, p: Partial<TimelineEntry>) => setRows((r) => r.map((x, j) => (j === i ? { ...x, ...p } : x)))
  const now = () => Number((src.current?.currentTime ?? 0).toFixed(1))
  const url = `/api/video/jobs/${job.id}/result.mp4`

  return (
    <>
      <Card>
        <CardHeader><CardTitle className="text-lg">Slide timeline</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          {job.note && (
            <Alert><AlertDescription className="flex items-center gap-2">
              <Badge variant={job.weak ? "destructive" : "secondary"}>{job.weak ? "Review needed" : "Matched"}</Badge>{job.note}
            </AlertDescription></Alert>
          )}
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-2">
              <video ref={src} src={`/api/video/jobs/${job.id}/source`} controls preload="metadata" className="w-full rounded-lg bg-black" />
              <p className="text-sm text-muted-foreground">Scrub the video, then press "Use current time" on a row to set when that slide appears. Before the first slide's time the presenter plays full-screen with the original background (intro); from then on the presenter sits in a white window at the bottom right, and any slide whose text would be hidden behind it is scaled down to fit beside it.</p>
            </div>
            <div className="space-y-2">
              {rows.map((r, i) => (
                <div key={i} className="flex flex-wrap items-center gap-2 rounded-lg border p-2">
                  <img src={job.slide_urls[r.slide - 1]} alt={`Slide ${r.slide}`} className="h-10 rounded border" />
                  <select aria-label="Slide" className={selectCls} value={r.slide} onChange={(e) => patch(i, { slide: Number(e.target.value) })}>
                    {Array.from({ length: n }, (_, k) => <option key={k} value={k + 1}>Slide {k + 1}</option>)}
                  </select>
                  <Input aria-label="Start time in seconds" type="number" min={0} step={0.1} className="w-24" value={r.start}
                    onChange={(e) => patch(i, { start: Number(e.target.value) })} />
                  <Button variant="outline" size="sm" onClick={() => patch(i, { start: now() })}>Use current time</Button>
                  <Button variant="ghost" size="icon" aria-label="Remove row" onClick={() => setRows((x) => x.filter((_, j) => j !== i))}>
                    <X aria-hidden className="size-4" />
                  </Button>
                </div>
              ))}
              <div className="flex flex-wrap items-center gap-3">
                <Button variant="outline" size="sm" onClick={() => setRows((x) => [...x, { slide: 1, start: now() }])}>
                  <Plus aria-hidden className="size-4" /> Add row
                </Button>
                <label className="flex items-center gap-2 text-sm">Background removal
                  <select className={selectCls} value={chroma} onChange={(e) => setChroma(e.target.value)}>
                    <option value="auto">Auto-detect solid colour</option><option value="on">Force on</option><option value="off">Keep original</option>
                  </select>
                </label>
              </div>
            </div>
          </div>
          <Button disabled={rendering || render.isPending || rows.length === 0} onClick={() => render.mutate({ timeline: rows, chroma })}>
            {rendering ? "Rendering..." : "Render video"}
          </Button>
          {render.error && <ErrorNote message={render.error.message} />}
          {job.segments && (
            <details><summary className="cursor-pointer text-sm font-medium">Transcript</summary>
              <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-muted p-3 text-xs">
                {job.segments.map((s) => `[${s.start.toFixed(1)}s] ${s.text}`).join("\n") || "(no speech detected)"}
              </pre>
            </details>
          )}
        </CardContent>
      </Card>
      {job.stage === "done" && job.result && (
        <Card>
          <CardHeader><CardTitle className="text-lg">Your synced video</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            <video src={`${url}?v=${job.result.duration}-${rows.map((r) => r.start).join()}#t=0.1`} controls preload="metadata" className="w-full rounded-lg bg-black" />
            <div className="flex flex-wrap items-center gap-3">
              <a className={buttonVariants()} href={`${url}?download=true`}>Download MP4</a>
              <span className="text-sm text-muted-foreground">
                {job.result.duration.toFixed(1)}s, {job.result.slides_used} slides, intro {job.result.intro_seconds ?? 0}s, presenter window bottom-right{job.result.shrunk_slides ? `, ${job.result.shrunk_slides} slide(s) scaled down to ${Math.round((job.result.min_slide_scale ?? 1) * 100)}% to keep text visible` : ""}, background {job.result.chroma_key ? "replaced with white" : "kept as is (not a solid colour)"}
              </span>
            </div>
          </CardContent>
        </Card>
      )}
    </>
  )
}
