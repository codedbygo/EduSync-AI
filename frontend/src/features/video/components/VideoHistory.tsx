import { useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button, buttonVariants } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ErrorNote, when } from "@/components/StatusCard"
import { useDeleteVideo, useVideoList } from "../hooks"

export function VideoHistory({ current, onOpen }: { current?: string; onOpen: (id: string | undefined) => void }) {
  const [limit, setLimit] = useState(20)
  const list = useVideoList(limit)
  const del = useDeleteVideo()
  const items = list.data?.items ?? []

  const remove = (id: string, label: string) => {
    if (!window.confirm(`Delete "${label}"? The uploaded video, slides and rendered MP4 are removed permanently.`)) return
    del.mutate(id, { onSuccess: () => (id === current ? onOpen(undefined) : undefined) })
  }

  return (
    <Card>
      <CardHeader><CardTitle className="text-lg">Video history{list.data ? ` (${list.data.total})` : ""}</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        {list.error && <ErrorNote message={list.error.message} />}
        {del.error && <ErrorNote message={del.error.message} />}
        {list.isPending && <p role="status" className="text-sm text-muted-foreground">Loading history...</p>}
        {list.data && items.length === 0 && <p className="text-sm text-muted-foreground">No videos yet. Each job you start is stored here.</p>}
        {items.length > 0 && (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Date</TableHead>
                  <TableHead>Video</TableHead>
                  <TableHead>Slides</TableHead>
                  <TableHead>Length</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead><span className="sr-only">Actions</span></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((i) => (
                  <TableRow key={i.id} aria-current={i.id === current ? "true" : undefined} className={i.id === current ? "bg-muted" : undefined}>
                    <TableCell className="whitespace-nowrap">{when(i.created_at)}</TableCell>
                    <TableCell>{i.video_name || i.title}<div className="text-xs text-muted-foreground">{i.deck_name}</div></TableCell>
                    <TableCell>{i.n_slides ?? "-"}</TableCell>
                    <TableCell>{i.duration != null ? `${i.duration.toFixed(1)}s` : "-"}</TableCell>
                    <TableCell>
                      <Badge variant={i.error ? "destructive" : i.has_result ? "secondary" : "outline"}>{i.has_result && i.stage === "done" ? "rendered" : i.stage}</Badge>
                    </TableCell>
                    <TableCell className="space-x-1 whitespace-nowrap text-right">
                      <Button size="sm" variant="outline" onClick={() => onOpen(i.id)}>Open</Button>
                      {i.has_result && (
                        <a className={buttonVariants({ size: "sm", variant: "outline" })} href={`/api/video/jobs/${i.id}/result.mp4?download=true`}>Download</a>
                      )}
                      <Button size="sm" variant="ghost" disabled={del.isPending} onClick={() => remove(i.id, i.video_name || i.title)}>Delete</Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        {list.data && list.data.total > items.length && (
          <Button variant="outline" size="sm" onClick={() => setLimit((l) => Math.min(l + 20, 100))} disabled={limit >= 100}>
            {limit >= 100 ? "Showing the latest 100" : "Show more"}
          </Button>
        )}
      </CardContent>
    </Card>
  )
}
