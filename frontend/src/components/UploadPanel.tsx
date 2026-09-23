import { useRef, useState, type DragEvent } from 'react'
import { api, type Run } from '../api'

interface Props {
  onRunStarted: (run: Run) => void
  canScreen: boolean
}

export default function UploadPanel({ onRunStarted, canScreen }: Props) {
  const [dragging, setDragging] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const [threshold, setThreshold] = useState(80)
  const [geoPenalty, setGeoPenalty] = useState(15)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  function accept(candidate: File | undefined) {
    if (!candidate) return
    if (!/\.(csv|txt)$/i.test(candidate.name)) {
      setError('Please upload a .csv file.')
      return
    }
    setError('')
    setFile(candidate)
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragging(false)
    accept(event.dataTransfer.files?.[0])
  }

  async function submit() {
    if (!file) return
    setBusy(true)
    setError('')
    try {
      const run = await api.uploadBatch(file, threshold, geoPenalty)
      setFile(null)
      if (inputRef.current) inputRef.current.value = ''
      onRunStarted(run)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <h2>Screen a partner batch</h2>
      <p className="sub">
        Drop a partner export to screen. Accepted columns:{' '}
        <span className="mono">GRID, Legal Name / Entity_Name, UBO_Name, Account Country, Tax_ID,
        Account Status, Created Date</span>. Column names are matched flexibly and the delimiter is
        detected automatically.
      </p>

      <div
        className={`dropzone ${dragging ? 'dragging' : ''}`}
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        onClick={() => inputRef.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === 'Enter' && inputRef.current?.click()}
      >
        <div className="big">{file ? file.name : 'Drop partner CSV here'}</div>
        <div>
          {file
            ? `${(file.size / 1_048_576).toFixed(1)} MB — click to choose a different file`
            : 'or click to browse'}
        </div>
        <input
          ref={inputRef}
          type="file"
          accept=".csv,text/csv"
          hidden
          onChange={(e) => accept(e.target.files?.[0])}
        />
      </div>

      <div className="row" style={{ marginTop: 16, alignItems: 'flex-end' }}>
        <div style={{ width: 220 }}>
          <label className="muted" style={{ fontSize: 13 }} htmlFor="threshold">
            Match threshold: <strong>{threshold}%</strong>
          </label>
          <input
            id="threshold"
            type="range"
            min={60}
            max={100}
            value={threshold}
            onChange={(e) => setThreshold(Number(e.target.value))}
          />
        </div>

        <div style={{ width: 220 }}>
          <label className="muted" style={{ fontSize: 13 }} htmlFor="geo">
            Geographic de-prioritisation: <strong>−{geoPenalty}</strong>
          </label>
          <input
            id="geo"
            type="range"
            min={0}
            max={40}
            value={geoPenalty}
            onChange={(e) => setGeoPenalty(Number(e.target.value))}
          />
        </div>

        <button className="primary" onClick={submit} disabled={!file || busy || !canScreen}>
          {busy ? 'Uploading…' : 'Run screening'}
        </button>
      </div>

      <p className="sub" style={{ marginTop: 12, marginBottom: 0 }}>
        Country contradiction lowers an alert's priority score but never removes it from the
        report, and never applies when either side's country is unknown.
      </p>

      {!canScreen && (
        <div className="banner warn" style={{ marginTop: 12 }}>
          Your role is read-only. Ask an administrator for analyst access to run screening.
        </div>
      )}
      {error && <div className="banner err" style={{ marginTop: 12 }}>{error}</div>}
    </div>
  )
}
