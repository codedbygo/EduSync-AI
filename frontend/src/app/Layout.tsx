import { Link, Outlet } from "@tanstack/react-router"
import { useQuery } from "@tanstack/react-query"
import { z } from "zod"
import { request } from "@/lib/api"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { useDrafts } from "./drafts"

const health = z.object({ llm_configured: z.boolean(), storage: z.string().optional(), database: z.string().optional() })
const tab = "rounded-t-lg px-4 py-2 text-sm font-semibold text-primary-foreground/80 hover:text-primary-foreground [&.active]:bg-background [&.active]:text-foreground"

export function Layout() {
  const { data } = useQuery({ queryKey: ["health"], queryFn: () => request("/api/health", health) })
  const lastVideo = useDrafts((s) => s.lastVideoJob)
  const lastEval = useDrafts((s) => s.lastEvalJob)
  return (
    <div className="min-h-screen bg-muted/40">
      <header className="bg-primary text-primary-foreground">
        <div className="mx-auto max-w-5xl px-4 pt-4">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <span className="text-xl font-extrabold tracking-tight">EduSync AI</span>
            <span className="text-sm opacity-80">Lecture video + slides sync, rubric-based answer grading</span>
          </div>
          <nav className="mt-3 flex gap-1" aria-label="Features">
            <Link to="/video" search={{ job: lastVideo }} className={tab} activeProps={{ className: "active" }}>Video + Slides</Link>
            <Link to="/eval" search={{ job: lastEval }} className={tab} activeProps={{ className: "active" }}>Answer Evaluation</Link>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-5xl space-y-4 px-4 py-6">
        {data && !data.llm_configured && (
          <Alert>
            <AlertDescription>
              No AI model is configured on this server (Gemini, OpenRouter or Ollama). Answer evaluation is disabled and slide matching falls back to the manual timeline.
            </AlertDescription>
          </Alert>
        )}
        <Outlet />
        {data?.storage === "local" && data.database === "sqlite" && (
          <p className="text-xs text-muted-foreground">Records are stored on this server's disk. Configure DATABASE_URL and Supabase Storage to keep them across redeploys.</p>
        )}
      </main>
    </div>
  )
}
