import { z } from "zod"
import { request } from "@/lib/api"
import { created, evalJob, evalList, rubricSchema } from "./schemas"

export type NewEval = { rubric: string; pages: File[]; student: string; title: string }

export const createEvalJob = (v: NewEval) => {
  const fd = new FormData()
  fd.append("rubric", v.rubric)
  fd.append("student", v.student.trim())
  fd.append("title", v.title.trim())
  v.pages.forEach((p) => fd.append("pages", p))
  return request("/api/eval/jobs", created, { method: "POST", body: fd })
}
export const fetchEvalJob = (id: string) => request(`/api/eval/jobs/${id}`, evalJob)
export const fetchEvalList = (limit: number) => request(`/api/eval/jobs?limit=${limit}`, evalList)
export const fetchSampleRubric = () => request("/api/eval/sample-rubric", rubricSchema)
export const fetchRubricFileText = (f: File) => {
  const fd = new FormData()
  fd.append("file", f)
  return request("/api/eval/rubric-text", z.object({ text: z.string() }), { method: "POST", body: fd }).then((r) => r.text)
}
export const retryEvalJob = (id: string) => request(`/api/eval/jobs/${id}/retry`, created, { method: "POST" })
export const deleteEvalJob = (id: string) => request(`/api/eval/jobs/${id}`, z.unknown(), { method: "DELETE" })

const path = (id: string, q: string, c: string) => `/api/eval/jobs/${id}/overrides/${encodeURIComponent(q)}/${encodeURIComponent(c)}`
export const setOverride = (id: string, q: string, c: string, marks: number, note: string) =>
  request(path(id, q, c), evalJob, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ marks, note }) })
export const clearOverride = (id: string, q: string, c: string) => request(path(id, q, c), evalJob, { method: "DELETE" })
