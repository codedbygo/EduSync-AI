import { request } from "@/lib/api"
import { created, evalJob, rubricSchema } from "./schemas"

export const createEvalJob = (rubric: string, pages: File[]) => {
  const fd = new FormData()
  fd.append("rubric", rubric)
  pages.forEach((p) => fd.append("pages", p))
  return request("/api/eval/jobs", created, { method: "POST", body: fd })
}
export const fetchEvalJob = (id: string) => request(`/api/eval/jobs/${id}`, evalJob)
export const fetchSampleRubric = () => request("/api/eval/sample-rubric", rubricSchema)
