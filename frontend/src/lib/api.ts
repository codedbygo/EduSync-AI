import { z } from "zod"

/** Single place that talks to the backend: checks status, validates the body with Zod. */
export async function request<T>(url: string, schema: z.ZodType<T>, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(url, init)
  } catch {
    throw new Error("Cannot reach the server. Is it running?")
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status})`
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (typeof body.detail === "string") detail = body.detail
      else if (Array.isArray(body.detail)) detail = body.detail.map((d: { msg?: string }) => d.msg).join("; ")
    } catch {
      /* keep default */
    }
    throw new Error(detail)
  }
  const parsed = schema.safeParse(res.status === 204 ? undefined : await res.json())
  if (!parsed.success) throw new Error("The server sent an unexpected response.")
  return parsed.data
}
