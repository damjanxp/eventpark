/**
 * Typed API clients generated from the backend's OpenAPI document (`make openapi`). If the backend renames a field,
 * `npm run typecheck` fails here instead of the dashboard breaking in production.
 */
import createClient from 'openapi-fetch'
import { driverAuth, problem, staffAuth } from './auth'
import type { paths } from './schema'

export const staffApi = createClient<paths>({ baseUrl: '', fetch: staffAuth.fetch })
export const driverApi = createClient<paths>({ baseUrl: '', fetch: driverAuth.fetch })

/** Unwrap an openapi-fetch result: data, or throw an ApiError the UI can display. */
export async function must<T>(call: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  const { data, response } = await call
  if (!response.ok) throw await problem(response.clone())
  return data as T
}
