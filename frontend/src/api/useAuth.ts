import { useEffect, useSyncExternalStore } from 'react'
import type { AuthState, AuthStore } from './auth'

/** Subscribe a component to an auth store; the first use after a page load tries the refresh cookie. */
export function useAuth(store: AuthStore): AuthState {
  const state = useSyncExternalStore(store.subscribe, store.getState)
  useEffect(() => {
    if (store.getState().status === 'loading') void store.refresh()
  }, [store])
  return state
}
