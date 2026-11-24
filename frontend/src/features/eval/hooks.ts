import { queryOptions, useMutation, useQuery } from "@tanstack/react-query"
import { createEvalJob, fetchEvalJob, fetchSampleRubric } from "./api"

export const evalKeys = { job: (id: string) => ["eval-job", id] as const, sample: ["sample-rubric"] as const }

export const useEvalJob = (id: string | undefined) =>
  useQuery({
    ...queryOptions({
      queryKey: evalKeys.job(id ?? ""),
      queryFn: () => fetchEvalJob(id ?? ""),
      refetchInterval: (q) => (q.state.data && (q.state.data.stage === "done" || q.state.data.error) ? false : 1500),
    }),
    enabled: !!id,
  })

export const useCreateEvalJob = () =>
  useMutation({ mutationFn: (v: { rubric: string; pages: File[] }) => createEvalJob(v.rubric, v.pages) })

export const useSampleRubric = () => useMutation({ mutationFn: fetchSampleRubric })
export { useQuery }
