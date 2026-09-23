import { useState, type FormEvent } from 'react'
import { api, type Session } from '../api'

export default function Login({ onSignedIn }: { onSignedIn: (session: Session) => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      onSignedIn(await api.login(email, password))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign-in failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={submit}>
        <h1>Sanctions Screening Engine</h1>
        <p className="sub muted">Delivery Hero Group — Compliance</p>

        <label htmlFor="email">Work email</label>
        <input
          id="email"
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />

        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />

        <button className="primary" style={{ width: '100%', marginTop: 20 }} disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>

        {error && <div className="error-text">{error}</div>}
      </form>
    </div>
  )
}
