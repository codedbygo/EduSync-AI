import { z } from "zod"

export const criterion = z.object({
  criterion_id: z.string(),
  description: z.string(),
  max_marks: z.number(),
  marks_awarded: z.number(),
  reasoning: z.string(),
  evidence_quote: z.string(),
  evidence_verified: z.boolean(),
  confidence: z.number(),
  needs_review: z.boolean(),
  flags: z.array(z.string()),
  second_grader_marks: z.number().nullable().optional(),
  graders_agree: z.boolean().nullable().optional(),
  teacher_marks: z.number().nullable(),
  teacher_note: z.string(),
  reviewed_at: z.string().nullable(),
  final_marks: z.number(),
})
export type Criterion = z.infer<typeof criterion>

export const questionResult = z.object({
  question_id: z.string(),
  question: z.string(),
  score: z.number(),
  final_score: z.number(),
  max_marks: z.number(),
  criteria: z.array(criterion),
  missing_concepts: z.array(z.string()),
  incorrect_statements: z.array(z.string()),
})
export const evalResult = z.object({
  total_score: z.number(),
  final_total: z.number(),
  open_reviews: z.number(),
  max_total: z.number(),
  needs_review: z.boolean(),
  second_opinion: z.boolean().optional(),
  ocr_confidence: z.number(),
  transcript: z.string(),
  questions: z.array(questionResult),
})
export type EvalResult = z.infer<typeof evalResult>

export const evalJob = z.object({
  id: z.string(),
  stage: z.string(),
  progress: z.number(),
  message: z.string(),
  error: z.string().nullable(),
  title: z.string(),
  student: z.string(),
  created: z.number(),
  page_urls: z.array(z.string()),
  storage_warning: z.string().optional(),
  result: evalResult.optional(),
})
export type EvalJob = z.infer<typeof evalJob>

export const evalListItem = z.object({
  id: z.string(),
  title: z.string(),
  stage: z.string(),
  created_at: z.number(),
  student: z.string().nullable().optional(),
  pages: z.number().nullable().optional(),
  ai_total: z.number().nullable().optional(),
  final_total: z.number().nullable().optional(),
  max_total: z.number().nullable().optional(),
  needs_review: z.boolean().nullable().optional(),
  open_reviews: z.number().nullable().optional(),
  error: z.string().nullable().optional(),
})
export const evalList = z.object({ items: z.array(evalListItem), total: z.number() })

export const created = z.object({ id: z.string() })
export const rubricSchema = z.object({ title: z.string().optional() }).passthrough()
