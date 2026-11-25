import { useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button, buttonVariants } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ErrorNote, when } from "@/components/StatusCard"
import { useDeleteEval, useEvalList } from "../hooks"

export function EvalHistory({ current, onOpen }: { current?: string; onOpen: (id: string | undefined) => void }) {
  const [limit, setLimit] = useState(20)
  const list = useEvalList(limit)
  const del = useDeleteEval()
  const items = list.data?.items ?? []

  const remove = (id: string, label: string) => {
    if (!window.confirm(`Delete the evaluation "${label}"? The answer sheet images, marks and review notes are removed permanently.`)) return
    del.mutate(id, { onSuccess: () => (id === current ? onOpen(undefined) : undefined) })
  }

  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
        <CardTitle className="text-lg">Evaluation history{list.data ? ` (${list.data.total})` : ""}</CardTitle>
        <a className={buttonVariants({ variant: "outline", size: "sm" })} href="/api/eval/export.csv">Export all as CSV</a>
      </CardHeader>
      <CardContent className="space-y-3">
        {list.error && <ErrorNote message={list.error.message} />}
        {del.error && <ErrorNote message={del.error.message} />}
        {list.isPending && <p role="status" className="text-sm text-muted-foreground">Loading history...</p>}
        {list.data && items.length === 0 && <p className="text-sm text-muted-foreground">No evaluations yet. Each one you run is stored here.</p>}
        {items.length > 0 && (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Date</TableHead>
                  <TableHead>Student</TableHead>
                  <TableHead>Title</TableHead>
                  <TableHead>Marks</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead><span className="sr-only">Actions</span></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((i) => (
                  <TableRow key={i.id} aria-current={i.id === current ? "true" : undefined} className={i.id === current ? "bg-muted" : undefined}>
                    <TableCell className="whitespace-nowrap">{when(i.created_at)}</TableCell>
                    <TableCell>{i.student || "(not given)"}</TableCell>
                    <TableCell>{i.title}</TableCell>
                    <TableCell className="whitespace-nowrap">
                      {i.final_total != null ? `${i.final_total} / ${i.max_total}` : "-"}
                      {i.final_total != null && i.ai_total != null && i.final_total !== i.ai_total ? ` (AI ${i.ai_total})` : ""}
                    </TableCell>
                    <TableCell>
                      {i.stage === "done"
                        ? (i.open_reviews ?? 0) > 0
                          ? <Badge variant="destructive">{i.open_reviews} to review</Badge>
                          : <Badge variant="secondary">{i.needs_review ? "Reviewed" : "Final"}</Badge>
                        : <Badge variant={i.error ? "destructive" : "outline"}>{i.stage}</Badge>}
                    </TableCell>
                    <TableCell className="space-x-1 whitespace-nowrap text-right">
                      <Button size="sm" variant="outline" onClick={() => onOpen(i.id)}>Open</Button>
                      <Button size="sm" variant="ghost" disabled={del.isPending} onClick={() => remove(i.id, i.student || i.title)}>Delete</Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        {list.data && list.data.total > items.length && (
          <Button variant="outline" size="sm" onClick={() => setLimit((l) => Math.min(l + 20, 100))} disabled={limit >= 100}>
            {limit >= 100 ? "Showing the latest 100 (export CSV for all)" : "Show more"}
          </Button>
        )}
      </CardContent>
    </Card>
  )
}
