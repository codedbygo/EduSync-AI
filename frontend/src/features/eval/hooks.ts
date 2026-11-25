import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { clearOverride, createEvalJob, deleteEvalJob, fetchEvalJob, fetchEvalList, fetchSampleRubric, retryEvalJob, setOverride, type NewEval } from "./api"

export const evalKeys = {
  all: ["eval"] as const,
  list: (limit: number) => ["eval", "list", limit] as const,
  job: (id: string) => ["eval", "job", id] as const,
  sample: ["eval", "sample-rubric"] as const,
}

const RUNNING = ["queued", "running"]

export const useEvalJob = (id: string | undefined) => {
  const qc = useQueryClient()
  return useQuery({
    ...queryOptions({
      queryKey: evalKeys.job(id ?? ""),
      queryFn: async () => {
        const j = await fetchEvalJob(id ?? "")
        if (!RUNNING.includes(j.stage)) void qc.invalidateQueries({ queryKey: ["eval", "list"] }) // history shows the finished state at once
        return j
      },
      refetchInterval: (q) => (!q.state.data || (RUNNING.includes(q.state.data.stage) && !q.state.data.error) ? 1500 : false),
    }),
    enabled: !!id,
  })
}

export const useEvalList = (limit: number) =>
  useQuery({
    queryKey: evalKeys.list(limit),
    queryFn: () => fetchEvalList(limit),
    refetchInterval: (q) => (q.state.data?.items.some((i) => RUNNING.includes(i.stage)) ? 3000 : false),
  })

export const useCreateEvalJob = () => {
  const qc = useQueryClient()
  return useMutation({ mutationFn: (v: NewEval) => createEvalJob(v), onSuccess: () => qc.invalidateQueries({ queryKey: evalKeys.all }) })
}

export const useRetryEval = () => {
  const qc = useQueryClient()
  return useMutation({ mutationFn: retryEvalJob, onSuccess: () => qc.invalidateQueries({ queryKey: evalKeys.all }) })
}

export const useDeleteEval = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: deleteEvalJob,
    onSuccess: (_, id) => {
      qc.removeQueries({ queryKey: evalKeys.job(id) })
      return qc.invalidateQueries({ queryKey: ["eval", "list"] })
    },
  })
}

export const useOverride = (id: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: { q: string; c: string; marks: number | null; note: string }) =>
      v.marks === null ? clearOverride(id, v.q, v.c) : setOverride(id, v.q, v.c, v.marks, v.note),
    onSuccess: (job) => {
      qc.setQueryData(evalKeys.job(id), job)
      return qc.invalidateQueries({ queryKey: ["eval", "list"] })
    },
  })
}

export const useSampleRubric = () => useMutation({ mutationFn: fetchSampleRubric })
