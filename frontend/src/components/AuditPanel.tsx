import { useEffect, useState } from 'react'
import { api, type ChainVerification } from '../api'

interface Stats {
  total_events: number
  by_action: { action: string; count: number }[]
}

export default function AuditPanel() {
  const [verification, setVerification] = useState<ChainVerification | null>(null)
  const [stats, setStats] = useState<Stats | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    api.auditStats().then(setStats).catch(() => setError('Could not load audit statistics'))
  }, [])

  async function verify() {
    setBusy(true)
    setError('')
    try {
      setVerification(await api.verifyChain())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Verification failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <h2>Audit trail integrity</h2>
      <p className="sub">
        Every watchlist refresh, screening run, adjudication and export is appended to a
        hash-chained log. Each entry commits to the hash of the one before it, so any edit or
        deletion of a historic record invalidates every subsequent hash and is detected here.
      </p>

      <div className="row">
        <button className="primary" onClick={verify} disabled={busy}>
          {busy ? 'Verifying…' : 'Verify chain'}
        </button>
        {stats && (
          <span className="muted">{stats.total_events.toLocaleString()} events recorded</span>
        )}
      </div>

      {error && <div className="banner err" style={{ marginTop: 12 }}>{error}</div>}

      {verification && (
        <div className={`banner ${verification.valid ? 'ok' : 'err'}`} style={{ marginTop: 12 }}>
          <strong>{verification.valid ? 'Chain intact' : 'TAMPERING DETECTED'}</strong>
          <div>
            {verification.events_checked.toLocaleString()} events verified — {verification.detail}
            {verification.broken_at !== null && ` (first break at sequence ${verification.broken_at})`}
          </div>
          {verification.head_hash && (
            <div className="mono" style={{ marginTop: 6 }}>
              head {verification.head_hash.slice(0, 32)}…
            </div>
          )}
        </div>
      )}

      {stats && stats.by_action.length > 0 && (
        <div className="table-wrap" style={{ marginTop: 16 }}>
          <table>
            <thead>
              <tr>
                <th>Event type</th>
                <th>Count</th>
              </tr>
            </thead>
            <tbody>
              {stats.by_action
                .slice()
                .sort((a, b) => b.count - a.count)
                .map((row) => (
                  <tr key={row.action}>
                    <td className="mono">{row.action}</td>
                    <td className="num">{row.count.toLocaleString()}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
