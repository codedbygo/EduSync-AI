import { create } from "zustand"
import { createJSONStorage, persist } from "zustand/middleware"

/** Client-only drafts: what the user has typed or picked but not submitted, and the job each tab last showed.
 * Text survives a reload (localStorage); picked files live in memory only (browsers cannot store File objects),
 * so they survive switching tabs but not a page reload. Server data never goes here (it lives in the query cache). */
type Drafts = {
  rubric: string
  student: string
  title: string
  pages: File[]
  pagePreviews: string[]
  video?: File
  deck?: File
  autoRender: boolean
  lastEvalJob?: string
  lastVideoJob?: string
  set: (p: Partial<Omit<Drafts, "set" | "setPages">>) => void
  setPages: (files: File[]) => void
}

const safeStorage = createJSONStorage(() => {
  try {
    return window.localStorage
  } catch {
    return { getItem: () => null, setItem: () => undefined, removeItem: () => undefined }
  }
})

export const useDrafts = create<Drafts>()(
  persist(
    (set, get) => ({
      rubric: "",
      student: "",
      title: "",
      pages: [],
      pagePreviews: [],
      autoRender: true,
      set: (p) => set(p),
      setPages: (files) => {
        get().pagePreviews.forEach((u) => URL.revokeObjectURL(u))
        set({ pages: files, pagePreviews: files.map((f) => URL.createObjectURL(f)) })
      },
    }),
    {
      name: "edusync-drafts",
      storage: safeStorage,
      partialize: (s) => ({ rubric: s.rubric, student: s.student, title: s.title, autoRender: s.autoRender, lastEvalJob: s.lastEvalJob, lastVideoJob: s.lastVideoJob }),
    },
  ),
)
