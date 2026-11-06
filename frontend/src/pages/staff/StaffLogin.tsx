import { CarFront } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router'
import { staffAuth } from '@/api/auth'
import { useAuth } from '@/api/useAuth'
import { homeFor } from '@/lib/roles'
import { Button } from '@/components/ui/button'
import { Card, CardBody } from '@/components/ui/card'
import { ErrorMessage } from '@/components/ui/feedback'
import { Field, Input } from '@/components/ui/input'

export function StaffLogin() {
  const auth = useAuth(staffAuth)
  const navigate = useNavigate()
  const from = (useLocation().state as { from?: string } | null)?.from
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  if (auth.status === 'authenticated') return <Navigate to={from ?? homeFor(auth.user.role)} replace />

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await staffAuth.login(email, password)
      const state = staffAuth.getState()
      if (state.status === 'authenticated') navigate(from ?? homeFor(state.user.role), { replace: true })
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardBody className="space-y-4 p-6">
          <div className="flex items-center gap-2 text-lg font-semibold">
            <CarFront className="size-6 text-brand" aria-hidden /> EventPark staff
          </div>
          <form onSubmit={submit} className="space-y-3">
            <Field label="Email">
              <Input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
            </Field>
            <Field label="Password">
              <Input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
            </Field>
            <ErrorMessage error={error} />
            <Button type="submit" className="w-full" disabled={busy}>
              Sign in
            </Button>
          </form>
          <p className="text-xs text-muted">
            Demo (after <code>make seed</code>): admin@eventpark.dev, gate.operator@eventpark.dev, parking.staff@eventpark.dev ·
            password eventpark-demo-pass
          </p>
        </CardBody>
      </Card>
    </div>
  )
}
