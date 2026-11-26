import { useNavigate, useSearch } from "@tanstack/react-router"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { ErrorNote, StatusCard, WarnNote } from "@/components/StatusCard"
import { useDrafts } from "@/app/drafts"
import { BUSY, useCreateVideoJob, useRetryVideo, useVideoJob } from "../hooks"
import { TimelineEditor } from "./TimelineEditor"
import { VideoHistory } from "./VideoHistory"

const fileCls = "block w-full rounded-lg border bg-background p-2 text-sm"
const TUNNEL = window.location.hostname.endsWith(".trycloudflare.com") // the free tunnel caps uploads at 100 MB

export function VideoPage() {
  const { job } = useSearch({ from: "/video" })
  const navigate = useNavigate({ from: "/video" })
  const d = useDrafts()
  const create = useCreateVideoJob()
  const retry = useRetryVideo()
  const q = useVideoJob(job)
  const s = q.data

  const open = (id: string | undefined) => {
    d.set({ lastVideoJob: id })
    return navigate({ search: { job: id } })
  }
  const start = () => {
    if (d.video && d.deck)
      create.mutate({ video: d.video, deck: d.deck, auto: d.autoRender }, {
        onSuccess: (r) => {
          d.set({ video: undefined, deck: undefined })
          return open(r.id)
        },
      })
  }
  const working = !!s && !s.error && BUSY.filter((x) => x !== "rendering").includes(s.stage)

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Sync a presenter video with its slides</CardTitle>
          <CardDescription>
            Upload the raw presenter video (green screen optional) and the deck. Speech is transcribed, matched to slides by meaning, then composited into one MP4 with your original audio. Every job and video is stored.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="vf">Presenter video (MP4, MOV, MKV, WebM)</Label>
              <input id="vf" type="file" accept="video/*" className={fileCls} onChange={(e) => d.set({ video: e.target.files?.[0] })} />
              {d.video && <p className="text-sm text-muted-foreground">Selected: {d.video.name} ({(d.video.size / 1048576).toFixed(1)} MB)</p>}
              {d.video && d.video.size > 100 * 1048576 && TUNNEL && (
                <WarnNote message="This video is over 100 MB, the upload limit of this public link, so the upload will be refused. Compress it first, for example to 720p." />
              )}
            </div>
            <div className="space-y-1">
              <Label htmlFor="df">Slides (PPTX, PPT, PDF)</Label>
              <input id="df" type="file" accept=".pptx,.ppt,.pdf,.odp" className={fileCls} onChange={(e) => d.set({ deck: e.target.files?.[0] })} />
              {d.deck && <p className="text-sm text-muted-foreground">Selected: {d.deck.name}</p>}
              {d.deck && !d.deck.name.toLowerCase().endsWith(".pdf") && (
                <p className="text-sm text-muted-foreground">Tip: export the deck as PDF first. PowerPoint files need a slow conversion on the server; a PDF skips it.</p>
              )}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-4">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={d.autoRender} onChange={(e) => d.set({ autoRender: e.target.checked })} />
              Render straight away when the match is confident
            </label>
            <Button onClick={start} disabled={!d.video || !d.deck || create.isPending || working}>
              {create.isPending ? "Uploading..." : "Start"}
            </Button>
          </div>
          {create.error && <ErrorNote message={create.error.message} />}
        </CardContent>
      </Card>

      {q.error && <ErrorNote message={q.error.message} />}
      {s && (s.error || BUSY.includes(s.stage)) && (
        <StatusCard message={s.message} progress={s.progress} error={s.error} interrupted={s.stage === "interrupted"}
          onRetry={() => retry.mutate(s.id)} retrying={retry.isPending} />
      )}
      {retry.error && <ErrorNote message={retry.error.message} />}
      {s?.storage_warning && <WarnNote message={s.storage_warning} />}
      {s && job && s.timeline && ["review", "done", "rendering", "error", "interrupted"].includes(s.stage) && (
        <TimelineEditor key={s.id} job={s} />
      )}

      <VideoHistory current={job} onOpen={open} />
    </div>
  )
}
