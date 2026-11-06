import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider } from 'react-router'
import { ApiError } from './api/auth'
import './index.css'
import { router } from './router'

// Server state (events, occupancy...) is a CACHE of backend data, owned by TanStack Query.
// Client state (open dialogs, form inputs) stays in components.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      // Don't retry what can't succeed (4xx); do retry network blips and 5xx.
      retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 3,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)
