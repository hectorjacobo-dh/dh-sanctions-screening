import { useCallback, useEffect, useState } from 'react'
import { api, getToken, setToken, type Run, type Session } from './api'
import AuditPanel from './components/AuditPanel'
import Login from './components/Login'
import RunDetail from './components/RunDetail'
import UploadPanel from './components/UploadPanel'
import WatchlistPanel from './components/WatchlistPanel'

type Tab = 'screening' | 'watchlists' | 'audit'

export default function App() {
  const [session, setSession] = useState<Session | null>(null)
  const [booting, setBooting] = useState(true)
  const [tab, setTab] = useState<Tab>('screening')
  const [runs, setRuns] = useState<Run[]>([])
  const [selected, setSelected] = useState<Run | null>(null)

  // Restore an existing session on reload.
  useEffect(() => {
    if (!getToken()) {
      setBooting(false)
      return
    }
    api
      .me()
      .then((me) =>
        setSession({
          access_token: getToken()!,
          token_type: 'bearer',
          email: me.email,
          role: me.role,
          full_name: me.full_name,
        }),
      )
      .catch(() => setToken(null))
      .finally(() => setBooting(false))
  }, [])

  const loadRuns = useCallback(async () => {
    if (!session) return
    try {
      const list = await api.listRuns()
      setRuns(list)
      setSelected((current) => current ?? list[0] ?? null)
    } catch {
      /* surfaced by the panels themselves */
    }
  }, [session])

  useEffect(() => {
    void loadRuns()
  }, [loadRuns])

  if (booting) return <div className="empty">Loading…</div>
  if (!session) return <Login onSignedIn={setSession} />

  const isAdmin = session.role === 'admin'
  const canScreen = isAdmin || session.role === 'analyst'

  function signOut() {
    setToken(null)
    setSession(null)
    setRuns([])
    setSelected(null)
  }

  return (
    <div className="app">
      <header className="topbar">
        <h1>DH Group — Sanctions Screening Engine</h1>
        <div className="spacer" />
        <span className="who">
          {session.email} · {session.role}
        </span>
        <button onClick={signOut}>Sign out</button>
      </header>

      <nav className="tabs">
        <button className={tab === 'screening' ? 'active' : ''} onClick={() => setTab('screening')}>
          Screening
        </button>
        <button className={tab === 'watchlists' ? 'active' : ''} onClick={() => setTab('watchlists')}>
          Watchlists
        </button>
        <button className={tab === 'audit' ? 'active' : ''} onClick={() => setTab('audit')}>
          Audit trail
        </button>
      </nav>

      <main className="content">
        {tab === 'watchlists' && <WatchlistPanel isAdmin={isAdmin} />}
        {tab === 'audit' && <AuditPanel />}

        {tab === 'screening' && (
          <>
            <WatchlistPanel isAdmin={isAdmin} />

            <UploadPanel
              canScreen={canScreen}
              onRunStarted={(run) => {
                setRuns((current) => [run, ...current])
                setSelected(run)
              }}
            />

            {runs.length > 0 && (
              <div className="card">
                <h2>Screening runs</h2>
                <p className="sub">Select a run to review and adjudicate its alerts.</p>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Started (UTC)</th>
                        <th>File</th>
                        <th>By</th>
                        <th>Status</th>
                        <th>Rows</th>
                        <th>Alerts</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {runs.map((run) => (
                        <tr key={run.run_ref}>
                          <td className="mono">{new Date(run.started_at).toISOString().slice(0, 19)}Z</td>
                          <td>{run.uploaded_filename}</td>
                          <td className="muted">{run.uploaded_by}</td>
                          <td>{run.status}</td>
                          <td className="num">{run.total_rows.toLocaleString()}</td>
                          <td className="num">{run.total_matches.toLocaleString()}</td>
                          <td>
                            <button onClick={() => setSelected(run)}>
                              {selected?.run_ref === run.run_ref ? 'Viewing' : 'Open'}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {selected && <RunDetail run={selected} canAdjudicate={canScreen} />}
          </>
        )}
      </main>
    </div>
  )
}
