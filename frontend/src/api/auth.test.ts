import { afterEach, expect, test, vi } from 'vitest'
import { AuthStore } from './auth'

const token = (n: number) => ({
  access_token: `t${n}`,
  token_type: 'bearer',
  expires_in: 900,
  user: { id: 'u', email: 'a@b.c', full_name: 'A', role: 'ADMIN', is_active: true, created_at: '' },
})

afterEach(() => vi.unstubAllGlobals())

test('ten 401s at once trigger ONE refresh, then every request is retried with the new token', async () => {
  let refreshes = 0
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    if (url.endsWith('/auth/refresh')) {
      refreshes++
      await new Promise((r) => setTimeout(r, 10))
      return new Response(JSON.stringify(token(refreshes)), { status: 200 })
    }
    const auth = input instanceof Request ? input.headers.get('Authorization') : new Headers(init?.headers).get('Authorization')
    return new Response('{}', { status: auth === 'Bearer t1' ? 200 : 401 })
  })
  vi.stubGlobal('fetch', fetchMock)
  const store = new AuthStore('/admin/v1')

  const responses = await Promise.all(
    Array.from({ length: 10 }, () => store.fetch(new Request('http://x/admin/v1/me'))),
  )

  expect(refreshes).toBe(1) // rotation would revoke the other 9 refresh attempts' tokens otherwise
  expect(responses.every((r) => r.status === 200)).toBe(true)
  expect(store.accessToken).toBe('t1')
})

test('a failed refresh signs the user out instead of looping', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 401 })))
  const store = new AuthStore('/admin/v1')
  const response = await store.fetch(new Request('http://x/admin/v1/me'))
  expect(response.status).toBe(401)
  expect(store.getState().status).toBe('anonymous')
})
