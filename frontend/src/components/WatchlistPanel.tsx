import { useCallback, useEffect, useState } from 'react'
import { api, type DashboardStatus } from '../api'

function hoursSince(iso: string | null): number | null {
  if (!iso) return null
  return (Date.now() - new Date(iso).getTime()) / 3_600_000
}

function freshness(iso: string | null, staleHours: number): 'ok' | 'warn' | 'err' {
  const age = hoursSince(iso)
  if (age === null) return 'err'
  return age > staleHours ? 'warn' : 'ok'
}

function formatAge(iso: string | null): string {
  const age = hoursSince(iso)
  if (age === null) return 'never refreshed'
  if (age < 1) return `${Math.round(age * 60)} min ago`
  if (age < 48) return `${Math.round(age)} h ago`
  return `${Math.round(age / 24)} days ago`
}

export default function WatchlistPanel({ isAdmin }: { isAdmin: boolean }) {
  const [status, setStatus] = useState<DashboardStatus | null>(null)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)

  const load = useCallback(async () => {
    try {
      setStatus(await api.watchlistStatus())
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load watchlist status')
    }
  }, [])

  useEffect(() => {
    void load()
    // Poll so a running refresh visibly lands without a manual reload.
    const timer = setInterval(() => void load(), 10_000)
    return () => clearInterval(timer)
  }, [load])

  async function refreshAll() {
    setRefreshing(true)
    try {
      await api.refreshWatchlists()
      setTimeout(() => void load(), 1500)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Refresh failed')
    } finally {
      setTimeout(() => setRefreshing(false), 1500)
    }
  }

  async function refreshOne(source: string) {
    try {
      await api.refreshOne(source)
      setTimeout(() => void load(), 1500)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Refresh failed')
    }
  }

  return (
    <div className="card">
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
        <div>
          <h2>Watchlist status</h2>
          <p className="sub" style={{ margin: 0 }}>
            Live designations consolidated from five regulator feeds.
            {status && ` ${status.total_targets.toLocaleString()} active targets.`}
          </p>
        </div>
        {isAdmin && (
          <button className="primary" onClick={refreshAll} disabled={refreshing}>
            {refreshing ? 'Refreshing…' : 'Refresh all lists'}
          </button>
        )}
      </div>

      {error && <div className="banner err">{error}</div>}

      {status?.any_source_stale && (
        <div className="banner warn">
          One or more lists have not refreshed successfully in the last{' '}
          {status.stale_threshold_hours} hours. Screening against a stale list carries
          retroactive liability — refresh before running a batch.
        </div>
      )}

      <div className="grid cols-5">
        {status?.sources.map((source) => {
          const state = freshness(source.last_success_utc, status.stale_threshold_hours)
          return (
            <div className="source-tile" key={source.source}>
              <div className="name">
                <span className={`dot ${state}`} />
                {source.source}
              </div>
              <div className="count">{source.record_count.toLocaleString()}</div>
              <div className="meta">{formatAge(source.last_success_utc)}</div>
              {source.payload_sha256 && (
                <div className="meta" title="SHA-256 of the exact published file ingested">
                  sha256 {source.payload_sha256.slice(0, 16)}…
                </div>
              )}
              {source.last_attempt_status === 'failed' && (
                <div className="meta" style={{ color: 'var(--danger)' }}>
                  last attempt failed: {source.last_error.slice(0, 120)}
                </div>
              )}
              {isAdmin && (
                <button style={{ marginTop: 10, width: '100%' }} onClick={() => refreshOne(source.source)}>
                  Refresh
                </button>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
