/**
 * Typed API client.
 *
 * The bearer token lives in sessionStorage rather than localStorage so it is
 * discarded when the tab closes — screening output is confidential and should
 * not persist on a shared workstation.
 */

const TOKEN_KEY = 'dh_screening_token'

export interface Session {
  access_token: string
  token_type: string
  email: string
  role: string
  full_name: string
}

export interface WatchlistStatus {
  source: string
  record_count: number
  last_success_utc: string | null
  payload_sha256: string
  source_url: string
  last_attempt_status: string
  last_attempt_utc: string | null
  last_error: string
}

export interface DashboardStatus {
  sources: WatchlistStatus[]
  total_targets: number
  oldest_successful_refresh_utc: string | null
  any_source_stale: boolean
  stale_threshold_hours: number
}

export interface Run {
  id: number
  run_ref: string
  uploaded_filename: string
  uploaded_by: string
  input_sha256: string
  started_at: string
  completed_at: string | null
  status: string
  progress: number
  threshold: number
  strong_threshold: number
  geo_penalty: number
  total_rows: number
  distinct_names: number
  total_matches: number
  error: string
}

export interface Match {
  id: number
  grid: string
  region: string
  account_country: string
  account_status: string
  created_date: string
  partner_legal_name: string
  ubo_name: string
  tax_id: string
  screened_field: string
  target_name: string
  entity_type: string
  sanctions_program: string
  remarks: string
  source: string
  geography: string
  dob: string
  watchlist_id: string
  score: number
  base_score: number
  geo_adjustment: number
  risk_band: string
  score_breakdown: Record<string, unknown>
  reason_codes: string
  decision: string
  comment: string
  grc_input: string
  decided_by: string
  decided_at: string | null
  screened_at_utc: string
}

export interface MatchPage {
  items: Match[]
  total: number
  limit: number
  offset: number
}

export interface ChainVerification {
  valid: boolean
  events_checked: number
  broken_at: number | null
  detail: string
  head_hash: string
}

export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null): void {
  if (token) sessionStorage.setItem(TOKEN_KEY, token)
  else sessionStorage.removeItem(TOKEN_KEY)
}

class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers = new Headers(init.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (init.body && !(init.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(path, { ...init, headers })

  if (response.status === 401) {
    setToken(null)
    throw new ApiError('Session expired. Please sign in again.', 401)
  }
  if (!response.ok) {
    let detail = `Request failed (${response.status})`
    try {
      const body = await response.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail, response.status)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  async login(email: string, password: string): Promise<Session> {
    const body = new URLSearchParams({ username: email, password })
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body,
    })
    if (!response.ok) {
      const detail = await response.json().catch(() => null)
      throw new ApiError(detail?.detail ?? 'Sign-in failed', response.status)
    }
    const session = (await response.json()) as Session
    setToken(session.access_token)
    return session
  },

  me: () =>
    request<{
      id: number
      email: string
      full_name: string
      role: string
      region: string
      is_active: boolean
    }>('/api/auth/me'),

  watchlistStatus: () => request<DashboardStatus>('/api/watchlists/status'),

  refreshWatchlists: () =>
    request<{ status: string; detail: string }>('/api/watchlists/refresh', { method: 'POST' }),

  refreshOne: (source: string) =>
    request<{ status: string; detail: string }>(
      `/api/watchlists/refresh/${encodeURIComponent(source)}`,
      { method: 'POST' },
    ),

  listRuns: () => request<Run[]>('/api/runs'),

  getRun: (runRef: string) => request<Run>(`/api/runs/${runRef}`),

  uploadBatch: (file: File, threshold: number, geoPenalty: number) => {
    const form = new FormData()
    form.append('file', file)
    form.append('threshold', String(threshold))
    form.append('geo_penalty', String(geoPenalty))
    return request<Run>('/api/runs', { method: 'POST', body: form })
  },

  listMatches: (runRef: string, params: Record<string, string | number>) => {
    const query = new URLSearchParams(
      Object.entries(params)
        .filter(([, v]) => v !== '' && v !== undefined && v !== null)
        .map(([k, v]) => [k, String(v)]),
    )
    return request<MatchPage>(`/api/runs/${runRef}/matches?${query}`)
  },

  summary: (runRef: string) => request<Record<string, any>>(`/api/runs/${runRef}/summary`),

  adjudicate: (runRef: string, matchId: number, payload: { decision: string; comment: string; grc_input: string }) =>
    request<Match>(`/api/runs/${runRef}/matches/${matchId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  verifyChain: () => request<ChainVerification>('/api/audit/verify'),

  auditStats: () => request<{ total_events: number; by_action: { action: string; count: number }[] }>(
    '/api/audit/stats',
  ),

  async downloadReport(runRef: string, filename: string): Promise<void> {
    const token = getToken()
    const response = await fetch(`/api/runs/${runRef}/report.csv`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!response.ok) throw new ApiError('Report download failed', response.status)

    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    URL.revokeObjectURL(url)
  },
}
