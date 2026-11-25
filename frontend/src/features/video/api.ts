import { z } from "zod"
import { request } from "@/lib/api"
import { created, videoJob, videoList, type TimelineEntry } from "./schemas"

export const createVideoJob = (video: File, deck: File, autoRender: boolean) => {
  const fd = new FormData()
  fd.append("video_file", video)
  fd.append("deck_file", deck)
  fd.append("auto_render", String(autoRender))
  return request("/api/video/jobs", created, { method: "POST", body: fd })
}

export const fetchVideoJob = (id: string) => request(`/api/video/jobs/${id}`, videoJob)
export const fetchVideoList = (limit: number) => request(`/api/video/jobs?limit=${limit}`, videoList)
export const retryVideoJob = (id: string) => request(`/api/video/jobs/${id}/retry`, created, { method: "POST" })
export const deleteVideoJob = (id: string) => request(`/api/video/jobs/${id}`, z.unknown(), { method: "DELETE" })

export const renderVideoJob = (id: string, timeline: TimelineEntry[], chroma: string) =>
  request(`/api/video/jobs/${id}/render`, created, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ timeline: timeline.map(({ slide, start }) => ({ slide, start })), chroma }),
  })
