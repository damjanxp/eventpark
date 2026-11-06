# EventPark frontend

One React SPA, three areas (docs/02-architecture.md §13):

| Path | Who | What |
|---|---|---|
| `/dashboard`, `/history` | all staff | live occupancy, KPIs, gate grid, hourly charts, cars inside, past events |
| `/operator` | ADMIN, GATE_OPERATOR (PARKING_STAFF read-only) | manual open, ticket lookup / void, lost ticket, live feed |
| `/setup` | ADMIN, EVENT_MANAGER | venues, zones, gates, device keys, events, pricing, zone capacity, reports, documents |
| `/book/*` | drivers | events → zone / VIP space → pay at fakepay → booking page with QR |

```bash
make up && make seed      # backend stack + demo data (repo root)
make web                  # http://localhost:5173  (staff: admin@eventpark.dev / eventpark-demo-pass)
npm test                  # vitest
npm run typecheck && npm run lint
make openapi              # after an API change: regenerate openapi.json + src/api/schema.d.ts
```

How it fits together
- **Same origin.** Vite proxies `/admin/v1`, `/public/v1`, `/ws/v1` to the API (CloudFront does it on AWS, ADR-029),
  so there's no CORS and the `SameSite=Strict` refresh cookie works.
- **Auth** (`src/api/auth.ts`): access token in memory only, refresh token in an HttpOnly cookie, one refresh in
  flight at a time, retry once on 401.
- **Typed client** (`src/api/client.ts`): `openapi-fetch` + types generated from the backend's OpenAPI. CI fails if
  they're stale.
- **Server state** in TanStack Query; **live updates** (`src/hooks/useEventStream.ts`) patch that cache from the
  WebSocket, with reconnect + backoff and 5-second polling while disconnected.
- UI pieces are small shadcn/ui-style components in `src/components/ui` (Tailwind v4, tokens in `src/index.css`).
