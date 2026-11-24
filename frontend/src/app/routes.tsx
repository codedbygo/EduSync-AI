import { createRootRoute, createRoute, createRouter, redirect } from "@tanstack/react-router"
import { z } from "zod"
import { Layout } from "./Layout"
import { VideoPage } from "@/features/video/components/VideoPage"
import { EvalPage } from "@/features/eval/components/EvalPage"

const search = z.object({ job: z.string().regex(/^[a-f0-9]{12}$/).optional().catch(undefined) })

const root = createRootRoute({ component: Layout })
const index = createRoute({ getParentRoute: () => root, path: "/", beforeLoad: () => { throw redirect({ to: "/video" }) } })
const video = createRoute({ getParentRoute: () => root, path: "/video", validateSearch: search, component: VideoPage })
const evaluation = createRoute({ getParentRoute: () => root, path: "/eval", validateSearch: search, component: EvalPage })

export const router = createRouter({ routeTree: root.addChildren([index, video, evaluation]) })

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router
  }
}
