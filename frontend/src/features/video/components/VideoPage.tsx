import { useState } from "react"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { ErrorNote, StatusCard } from "@/components/StatusCard"
import { useCreateVideoJob, useVideoJob } from "../hooks"
import { TimelineEditor } from "./TimelineEditor"

const fileCls = "block w-full rounded-lg border bg-background p-2 text-sm"

export function VideoPage() {
  const { job } = useSearch({ from: "/video" })
  const navigate = useNavigate({ from: "/video" })
  const [video, setVideo] = useState<File>()
  const [deck, setDeck] = useState<File>()
  const [auto, setAuto] = useState(true)
  const create = useCreateVideoJob()
  const q = useVideoJob(job)
  const s = q.data

  const start = () => {
    if (video && deck) create.mutate({ video, deck, auto }, { onSuccess: (r) => navigate({ search: { job: r.id } }) })
  }
  const working = !!s && !s.error && ["queued", "slides", "transcribing", "matching"].includes(s.stage)

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Sync a presenter video with its slides</CardTitle>
          <CardDescription>
            Upload the raw presenter video (green screen optional) and the deck. Speech is transcribed locally, matched to slides by meaning, then composited into one MP4 with your original audio.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="vf">Presenter video (MP4, MOV, MKV, WebM)</Label>
              <input id="vf" type="file" accept="video/*" className={fileCls} onChange={(e) => setVideo(e.target.files?.[0])} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="df">Slides (PPTX, PPT, PDF)</Label>
              <input id="df" type="file" accept=".pptx,.ppt,.pdf,.odp" className={fileCls} onChange={(e) => setDeck(e.target.files?.[0])} />
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-4">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
              Render straight away when the match is confident
            </label>
            <Button onClick={start} disabled={!video || !deck || create.isPending || working}>
              {create.isPending ? "Uploading..." : "Start"}
            </Button>
          </div>
          {create.error && <ErrorNote message={create.error.message} />}
        </CardContent>
      </Card>

      {q.error && <ErrorNote message={q.error.message} />}
      {s && (s.error || ["queued", "slides", "transcribing", "matching", "rendering"].includes(s.stage)) && (
        <StatusCard message={s.message} progress={s.stage === "rendering" ? s.progress : s.progress} error={s.error} />
      )}
      {s && job && s.timeline && (s.stage === "review" || s.stage === "done" || s.stage === "rendering" || s.error) && (
        <TimelineEditor key={s.id} job={s} />
      )}
    </div>
  )
}
