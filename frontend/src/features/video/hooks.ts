import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { createVideoJob, fetchVideoJob, renderVideoJob } from "./api"
import type { TimelineEntry } from "./schemas"

export const videoKeys = { job: (id: string) => ["video-job", id] as const }

const busy = ["queued", "slides", "transcribing", "matching", "rendering"]

export const videoJobOptions = (id: string) =>
  queryOptions({
    queryKey: videoKeys.job(id),
    queryFn: () => fetchVideoJob(id),
    refetchInterval: (q) => {
      const s = q.state.data
      return !s || (busy.includes(s.stage) && !s.error) ? 1200 : false
    },
  })

export const useVideoJob = (id: string | undefined) =>
  useQuery({ ...videoJobOptions(id ?? ""), enabled: !!id })

export const useCreateVideoJob = () =>
  useMutation({ mutationFn: (v: { video: File; deck: File; auto: boolean }) => createVideoJob(v.video, v.deck, v.auto) })

export const useRender = (id: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: { timeline: TimelineEntry[]; chroma: string }) => renderVideoJob(id, v.timeline, v.chroma),
    onSuccess: () => qc.invalidateQueries({ queryKey: videoKeys.job(id) }),
  })
}
