import { useState, type FormEvent } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router'
import { driverAuth } from '@/api/auth'
import { useAuth } from '@/api/useAuth'
import { Button } from '@/components/ui/button'
import { Card, CardBody } from '@/components/ui/card'
import { ErrorMessage } from '@/components/ui/feedback'
import { Field, Input } from '@/components/ui/input'

export function DriverLogin() {
  const auth = useAuth(driverAuth)
  const navigate = useNavigate()
  const from = (useLocation().state as { from?: string } | null)?.from ?? '/book/bookings'
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  if (auth.status === 'authenticated') return <Navigate to={from} replace />

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      if (mode === 'login') await driverAuth.login(email, password)
      else await driverAuth.register(email, password, name)
      navigate(from, { replace: true })
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="mx-auto max-w-sm">
      <CardBody className="space-y-4 p-6">
        <h1 className="text-lg font-semibold">{mode === 'login' ? 'Sign in' : 'Create an account'}</h1>
        <form onSubmit={submit} className="space-y-3">
          {mode === 'register' && (
            <Field label="Full name">
              <Input required value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
            </Field>
          )}
          <Field label="Email">
            <Input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" />
          </Field>
          <Field label="Password" hint={mode === 'register' ? 'At least 12 characters.' : undefined}>
            <Input
              type="password"
              required
              minLength={mode === 'register' ? 12 : undefined}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            />
          </Field>
          <ErrorMessage error={error} />
          <Button type="submit" className="w-full" disabled={busy}>
            {mode === 'login' ? 'Sign in' : 'Create account'}
          </Button>
        </form>
        <button className="text-sm text-brand hover:underline" onClick={() => setMode(mode === 'login' ? 'register' : 'login')}>
          {mode === 'login' ? 'New here? Create an account' : 'Already have an account? Sign in'}
        </button>
        {mode === 'login' && <p className="text-xs text-muted">Demo: driver@eventpark.dev / eventpark-demo-pass</p>}
      </CardBody>
    </Card>
  )
}
