import { z } from "zod"

export const timelineEntry = z.object({
  slide: z.number().int(),
  start: z.number(),
  confidence: z.number().optional(),
  reason: z.string().optional(),
})
export type TimelineEntry = z.infer<typeof timelineEntry>

export const segment = z.object({ start: z.number(), end: z.number(), text: z.string() })

export const videoJob = z.object({
  id: z.string(),
  stage: z.string(),
  progress: z.number(),
  message: z.string(),
  error: z.string().nullable(),
  n_slides: z.number().optional(),
  slide_urls: z.array(z.string()),
  segments: z.array(segment).optional(),
  timeline: z.array(timelineEntry).optional(),
  weak: z.boolean().optional(),
  note: z.string().optional(),
  result: z
    .object({ duration: z.number(), chroma_key: z.string().nullable(), fade: z.number(), slides_used: z.number(), intro_seconds: z.number().optional(), presenter_window: z.string().optional(), shrunk_slides: z.number().optional(), min_slide_scale: z.number().optional() })
    .nullable()
    .optional(),
})
export type VideoJob = z.infer<typeof videoJob>
export const created = z.object({ id: z.string() })
