import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { createVideoJob, deleteVideoJob, fetchVideoJob, fetchVideoList, renderVideoJob, retryVideoJob } from "./api"
import type { TimelineEntry } from "./schemas"

export const videoKeys = {
  all: ["video"] as const,
  list: (limit: number) => ["video", "list", limit] as const,
  job: (id: string) => ["video", "job", id] as const,
}

export const BUSY = ["queued", "saving", "slides", "transcribing", "matching", "rendering"]

export const videoJobOptions = (id: string) =>
  queryOptions({
    queryKey: videoKeys.job(id),
    queryFn: () => fetchVideoJob(id),
    refetchInterval: (q) => {
      const s = q.state.data
      return !s || (BUSY.includes(s.stage) && !s.error) ? 1200 : false
    },
  })

export const useVideoJob = (id: string | undefined) => {
  const qc = useQueryClient()
  return useQuery({
    ...videoJobOptions(id ?? ""),
    queryFn: async () => {
      const j = await fetchVideoJob(id ?? "")
      if (!BUSY.includes(j.stage)) void qc.invalidateQueries({ queryKey: ["video", "list"] }) // history shows the finished state at once
      return j
    },
    enabled: !!id,
  })
}

export const useVideoList = (limit: number) =>
  useQuery({
    queryKey: videoKeys.list(limit),
    queryFn: () => fetchVideoList(limit),
    refetchInterval: (q) => (q.state.data?.items.some((i) => BUSY.includes(i.stage)) ? 3000 : false),
  })

const useInvalidate = () => {
  const qc = useQueryClient()
  return () => qc.invalidateQueries({ queryKey: videoKeys.all })
}

export const useCreateVideoJob = () => {
  const inv = useInvalidate()
  return useMutation({ mutationFn: (v: { video: File; deck: File; auto: boolean }) => createVideoJob(v.video, v.deck, v.auto), onSuccess: inv })
}

export const useRetryVideo = () => {
  const inv = useInvalidate()
  return useMutation({ mutationFn: retryVideoJob, onSuccess: inv })
}

export const useDeleteVideo = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: deleteVideoJob,
    onSuccess: (_, id) => {
      qc.removeQueries({ queryKey: videoKeys.job(id) })
      return qc.invalidateQueries({ queryKey: ["video", "list"] })
    },
  })
}

export const useRender = (id: string) => {
  const inv = useInvalidate()
  return useMutation({ mutationFn: (v: { timeline: TimelineEntry[]; chroma: string }) => renderVideoJob(id, v.timeline, v.chroma), onSuccess: inv })
}
