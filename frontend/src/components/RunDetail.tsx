import { useCallback, useEffect, useState } from 'react'
import { api, type Match, type Run } from '../api'

const DECISIONS = ['', 'Positive Match', 'Potential Match', 'False Match']

function bandClass(band: string): string {
  if (band.startsWith('Strong')) return 'strong'
  if (band.startsWith('Potential')) return 'potential'
  return 'weak'
}

export default function RunDetail({ run, canAdjudicate }: { run: Run; canAdjudicate: boolean }) {
  const [live, setLive] = useState<Run>(run)
  const [matches, setMatches] = useState<Match[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [filters, setFilters] = useState({ region: '', source: '', risk_band: '', min_score: '' })
  const [error, setError] = useState('')
  const limit = 100

  const isRunning = live.status === 'queued' || live.status === 'running'

  // Poll the run itself while it is still executing.
  useEffect(() => {
    setLive(run)
  }, [run])

  useEffect(() => {
    if (!isRunning) return
    const timer = setInterval(async () => {
      try {
        setLive(await api.getRun(run.run_ref))
      } catch {
        /* transient */
      }
    }, 2000)
    return () => clearInterval(timer)
  }, [isRunning, run.run_ref])

  const loadMatches = useCallback(async () => {
    if (live.status !== 'completed') return
    try {
      const page = await api.listMatches(live.run_ref, { limit, offset, ...filters })
      setMatches(page.items)
      setTotal(page.total)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load matches')
    }
  }, [live.status, live.run_ref, offset, filters])

  useEffect(() => {
    void loadMatches()
  }, [loadMatches])

  async function save(match: Match, patch: Partial<Pick<Match, 'decision' | 'comment' | 'grc_input'>>) {
    const next = {
      decision: patch.decision ?? match.decision,
      comment: patch.comment ?? match.comment,
      grc_input: patch.grc_input ?? match.grc_input,
    }
    // Optimistic: the analyst keeps typing while the write lands.
    setMatches((current) => current.map((m) => (m.id === match.id ? { ...m, ...next } : m)))
    try {
      await api.adjudicate(live.run_ref, match.id, next)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save decision')
      void loadMatches()
    }
  }

  if (isRunning) {
    return (
      <div className="card">
        <h2>Screening in progress</h2>
        <p className="sub">
          {live.uploaded_filename} — {live.distinct_names.toLocaleString()} distinct names indexed
          from {live.total_rows.toLocaleString()} rows.
        </p>
        <div className="progress">
          <div style={{ width: `${live.progress}%` }} />
        </div>
        <p className="muted" style={{ marginTop: 8 }}>{live.progress.toFixed(1)}% complete</p>
      </div>
    )
  }

  if (live.status === 'failed') {
    return (
      <div className="card">
        <h2>Screening failed</h2>
        <div className="banner err">{live.error || 'Unknown error'}</div>
      </div>
    )
  }

  return (
    <div className="card">
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div>
          <h2>{live.uploaded_filename}</h2>
          <p className="sub" style={{ margin: 0 }}>
            {live.total_matches.toLocaleString()} alerts from{' '}
            {live.total_rows.toLocaleString()} rows ({live.distinct_names.toLocaleString()} distinct
            names) · threshold {live.threshold}% · run{' '}
            <span className="mono">{live.run_ref.slice(0, 8)}</span>
          </p>
        </div>
        <button
          className="primary"
          onClick={() =>
            api.downloadReport(live.run_ref, `DH_Adjudication_${live.run_ref.slice(0, 8)}.csv`)
          }
        >
          Download adjudication report
        </button>
      </div>

      <p className="sub mono" style={{ marginTop: 8 }}>
        input sha256 {live.input_sha256.slice(0, 32)}…
      </p>

      {error && <div className="banner err">{error}</div>}

      <div className="row" style={{ margin: '14px 0' }}>
        {(['region', 'source', 'risk_band'] as const).map((key) => (
          <input
            key={key}
            placeholder={`Filter ${key.replace('_', ' ')}`}
            style={{ width: 180 }}
            value={filters[key]}
            onChange={(e) => {
              setOffset(0)
              setFilters((f) => ({ ...f, [key]: e.target.value }))
            }}
          />
        ))}
        <input
          type="number"
          placeholder="Min score"
          style={{ width: 120 }}
          value={filters.min_score}
          onChange={(e) => {
            setOffset(0)
            setFilters((f) => ({ ...f, min_score: e.target.value }))
          }}
        />
        <span className="muted">{total.toLocaleString()} alerts</span>
      </div>

      {matches.length === 0 ? (
        <div className="empty">No alerts match these filters.</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Score</th>
                <th>Partner</th>
                <th>Designated target</th>
                <th>Source</th>
                <th>Geography</th>
                <th>Decision</th>
                <th>Comment</th>
                <th>GRC input</th>
              </tr>
            </thead>
            <tbody>
              {matches.map((match) => (
                <tr key={match.id}>
                  <td className="num">
                    <div>
                      <span className={`badge ${bandClass(match.risk_band)}`}>
                        {match.score.toFixed(0)}
                      </span>
                    </div>
                    {match.geo_adjustment !== 0 && (
                      <div style={{ marginTop: 4 }}>
                        <span className="badge geo" title="De-prioritised: partner country contradicts the designation">
                          geo {match.geo_adjustment}
                        </span>
                      </div>
                    )}
                  </td>
                  <td>
                    <strong>{match.partner_legal_name || match.ubo_name}</strong>
                    <div className="muted mono">
                      {match.grid} · {match.account_country || 'country unknown'} · {match.region}
                    </div>
                    {match.screened_field === 'UBO' && (
                      <div className="muted">matched on UBO</div>
                    )}
                  </td>
                  <td>
                    <strong>{match.target_name}</strong>
                    <div className="muted">{match.sanctions_program}</div>
                    <div className="reasons">
                      {match.reason_codes
                        .split(',')
                        .filter(Boolean)
                        .map((code) => (
                          <span key={code}>{code}</span>
                        ))}
                    </div>
                  </td>
                  <td>
                    {match.source}
                    <div className="muted mono">{match.watchlist_id}</div>
                  </td>
                  <td className="muted">{match.geography || '—'}</td>
                  <td>
                    <select
                      value={match.decision}
                      disabled={!canAdjudicate}
                      onChange={(e) => save(match, { decision: e.target.value })}
                    >
                      {DECISIONS.map((d) => (
                        <option key={d} value={d}>
                          {d || 'Unreviewed'}
                        </option>
                      ))}
                    </select>
                    {match.decided_by && (
                      <div className="muted mono" style={{ marginTop: 4 }}>
                        {match.decided_by}
                      </div>
                    )}
                  </td>
                  <td>
                    <textarea
                      rows={2}
                      style={{ minWidth: 180 }}
                      disabled={!canAdjudicate}
                      defaultValue={match.comment}
                      onBlur={(e) => {
                        if (e.target.value !== match.comment) save(match, { comment: e.target.value })
                      }}
                    />
                  </td>
                  <td>
                    <textarea
                      rows={2}
                      style={{ minWidth: 160 }}
                      disabled={!canAdjudicate}
                      defaultValue={match.grc_input}
                      onBlur={(e) => {
                        if (e.target.value !== match.grc_input)
                          save(match, { grc_input: e.target.value })
                      }}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="row" style={{ marginTop: 12, justifyContent: 'space-between' }}>
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>
          Previous
        </button>
        <span className="muted">
          {offset + 1}–{Math.min(offset + limit, total)} of {total.toLocaleString()}
        </span>
        <button disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}>
          Next
        </button>
      </div>
    </div>
  )
}
