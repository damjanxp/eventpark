/**
 * Browser auth (docs/02 §9, learning path Phase 8 "XSS vs CSRF").
 *
 * - The ACCESS token (15 min JWT) lives only in this module's memory. Not in localStorage: an injected script
 *   (XSS) could read localStorage forever; memory is gone when the tab closes.
 * - The REFRESH token is an HttpOnly + SameSite=Strict cookie set by the API on /{realm}/v1/auth. JavaScript can't
 *   read it (XSS can't steal it), and the browser doesn't send it on requests started by other sites (CSRF).
 * - After a page reload we have no access token, so we call /auth/refresh: the cookie proves who we are and we get
 *   a fresh access token (and a rotated refresh cookie).
 *
 * Two realms with separate cookies: staff (/admin/v1) and drivers (/public/v1).
 */
import type { components } from './schema'

export type User = components['schemas']['UserOut']
export type Role = User['role']
type TokenResponse = components['schemas']['TokenResponse']

export type AuthState =
  | { status: 'loading' }
  | { status: 'anonymous' }
  | { status: 'authenticated'; user: User; accessToken: string; expiresAt: number }

export class AuthStore {
  private state: AuthState = { status: 'loading' }
  private listeners = new Set<() => void>()
  private refreshing: Promise<boolean> | null = null
  private timer: ReturnType<typeof setTimeout> | undefined

  readonly prefix: '/admin/v1' | '/public/v1'

  constructor(prefix: '/admin/v1' | '/public/v1') {
    this.prefix = prefix
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  getState = (): AuthState => this.state

  get accessToken(): string | null {
    return this.state.status === 'authenticated' ? this.state.accessToken : null
  }

  private set(state: AuthState) {
    this.state = state
    clearTimeout(this.timer)
    if (state.status === 'authenticated') {
      // Refresh a minute before expiry, so normal requests never see a 401.
      const inMs = Math.max(5_000, state.expiresAt - Date.now() - 60_000)
      this.timer = setTimeout(() => void this.refresh(), inMs)
    }
    this.listeners.forEach((l) => l())
  }

  private accept(body: TokenResponse) {
    this.set({
      status: 'authenticated',
      user: body.user,
      accessToken: body.access_token,
      expiresAt: Date.now() + body.expires_in * 1000,
    })
  }

  async login(email: string, password: string): Promise<void> {
    const response = await fetch(`${this.prefix}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    })
    if (!response.ok) throw await problem(response)
    this.accept(await response.json())
  }

  async register(email: string, password: string, fullName: string): Promise<void> {
    const response = await fetch(`${this.prefix}/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password, full_name: fullName }),
    })
    if (!response.ok) throw await problem(response)
    this.accept(await response.json())
  }

  /** Single-flight: ten requests failing with 401 at once trigger ONE refresh (rotation would revoke the rest). */
  refresh(): Promise<boolean> {
    this.refreshing ??= (async () => {
      try {
        const response = await fetch(`${this.prefix}/auth/refresh`, { method: 'POST' })
        if (!response.ok) {
          this.set({ status: 'anonymous' })
          return false
        }
        this.accept(await response.json())
        return true
      } catch {
        // Network error: keep the current state; the next request will try again.
        if (this.state.status === 'loading') this.set({ status: 'anonymous' })
        return false
      } finally {
        this.refreshing = null
      }
    })()
    return this.refreshing
  }

  async logout(): Promise<void> {
    await fetch(`${this.prefix}/auth/logout`, { method: 'POST' }).catch(() => undefined)
    this.set({ status: 'anonymous' })
  }

  /** fetch() with the bearer token; on 401 refresh once and retry. Used by the generated API client. */
  fetch = async (request: Request): Promise<Response> => {
    const retry = request.clone() // a body can be read only once
    const first = await fetch(this.withToken(request))
    if (first.status !== 401 || !(await this.refresh())) return first
    return fetch(this.withToken(retry))
  }

  private withToken(request: Request): Request {
    const token = this.accessToken
    if (token) request.headers.set('Authorization', `Bearer ${token}`)
    return request
  }
}

/** RFC 9457 problem+json from the API, as an Error the UI can show. */
export class ApiError extends Error {
  readonly status: number
  readonly code?: string
  readonly requestId?: string

  constructor(message: string, status: number, code?: string, requestId?: string) {
    super(message)
    this.status = status
    this.code = code
    this.requestId = requestId
  }
}

export async function problem(response: Response): Promise<ApiError> {
  try {
    const body = await response.json()
    const detail = typeof body.detail === 'string' ? body.detail : (body.title ?? response.statusText)
    return new ApiError(detail, response.status, body.code, body.request_id ?? response.headers.get('x-request-id'))
  } catch {
    return new ApiError(response.statusText || 'Request failed', response.status)
  }
}

export const staffAuth = new AuthStore('/admin/v1')
export const driverAuth = new AuthStore('/public/v1')
