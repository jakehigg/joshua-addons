export interface Settings {
  manager_label: string
  xp_per_dollar: number
  cooldown_seconds: number
  tz: string
}

export interface Member {
  slug: string
  name: string
  is_active: boolean
  sort_order: number
  balance: number
  balance_dollars: string | null
}

export type Frequency = 'daily' | 'weekly' | 'monthly' | 'one_off'

export const FREQUENCY_LABEL: Record<Frequency, string> = {
  daily: 'Daily',
  weekly: 'Weekly',
  monthly: 'Monthly',
  one_off: 'One-off',
}

export interface Chore {
  id: number
  name: string
  points: number
  frequency: Frequency
  is_active: boolean
  next_due_date: string
  last_completed_at: string | null
}

// A chore row from GET /api/chores. The server sets `overdue`.
export interface ChoreRow extends Chore {
  slug: string
  overdue: boolean
}

export interface ChoreList {
  today: string
  chores: ChoreRow[]
}

export interface CompleteResult {
  chore_id: number
  chore_name: string
  completion_id: number
  points_awarded: number
  balance: number
  balance_dollars: string | null
  next_due_date: string | null
  is_active: boolean
}

export interface Transaction {
  id: number
  amount: number
  description: string
  source: string
  created_at: string
}

export interface LedgerResult {
  slug: string
  transaction: Transaction
  balance: number
  balance_dollars: string | null
}

export interface ChoreChanges {
  name?: string
  points?: number
  frequency?: Frequency
  next_due_date?: string
  is_active?: boolean
}

export interface MemberChanges {
  name?: string
  is_active?: boolean
  sort_order?: number
}

// An API error. `status` is the HTTP status, `message` is the server's detail.
export class ApiError extends Error {
  status: number
  retryAfter: number | null

  constructor(status: number, message: string, retryAfter: number | null) {
    super(message)
    this.status = status
    this.retryAfter = retryAfter
  }
}

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(path, options)
  if (!res.ok) {
    const text = await res.text()
    let message = `${res.status}: ${text}`
    try {
      const body: unknown = JSON.parse(text)
      if (body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string') {
        message = body.detail
      }
    } catch {
      // The body is not JSON. Keep the status and the text.
    }
    const retry = Number.parseInt(res.headers.get('Retry-After') ?? '', 10)
    throw new ApiError(res.status, message, Number.isNaN(retry) ? null : retry)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

function managerHeaders(pin: string, json = false): HeadersInit {
  const headers: Record<string, string> = { Authorization: `Bearer ${pin}` }
  if (json) headers['Content-Type'] = 'application/json'
  return headers
}

function query(params: Record<string, string | number | boolean | undefined>): string {
  const q = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) q.set(key, String(value))
  }
  const s = q.toString()
  return s ? `?${s}` : ''
}

const enc = encodeURIComponent

// The date today in the browser's time zone, as YYYY-MM-DD.
export function localToday(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

export function formatDate(isoDate: string): string {
  const d = new Date(isoDate + 'T00:00:00')
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', weekday: 'short' })
}

export function formatDateTime(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) +
    ' ' + d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
}

// Use this only for a chore row that has no `overdue` flag from the server.
export function isOverdue(isoDate: string): boolean {
  return isoDate < localToday()
}

export function choreOverdue(chore: Chore | ChoreRow): boolean {
  return 'overdue' in chore ? chore.overdue : isOverdue(chore.next_due_date)
}

export const api = {
  getSettings: () => apiFetch<Settings>('/api/settings'),

  getVersion: () => apiFetch<{ version: string }>('/version'),

  getMembers: (includeInactive = false) =>
    apiFetch<Member[]>(`/api/members${query({ include_inactive: includeInactive || undefined })}`),

  getMember: (slug: string) => apiFetch<Member>(`/api/members/${enc(slug)}`),

  getLedger: (slug: string, limit = 50) =>
    apiFetch<Transaction[]>(`/api/members/${enc(slug)}/ledger${query({ limit })}`),

  getChores: (slug?: string) => apiFetch<ChoreList>(`/api/chores${query({ slug })}`),

  completeChore: (choreId: number, note?: string) =>
    apiFetch<CompleteResult>(`/api/chores/${choreId}/complete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(note ? { note } : {}),
    }),

  createMember: (pin: string, body: { slug: string; name: string }) =>
    apiFetch<Member>('/api/members', {
      method: 'POST',
      headers: managerHeaders(pin, true),
      body: JSON.stringify(body),
    }),

  updateMember: (pin: string, slug: string, body: MemberChanges) =>
    apiFetch<Member>(`/api/members/${enc(slug)}`, {
      method: 'PATCH',
      headers: managerHeaders(pin, true),
      body: JSON.stringify(body),
    }),

  createChore: (pin: string, body: {
    slug: string; name: string; points: number; frequency: Frequency; next_due_date?: string
  }) =>
    apiFetch<Chore & { slug: string }>('/api/chores', {
      method: 'POST',
      headers: managerHeaders(pin, true),
      body: JSON.stringify(body),
    }),

  updateChore: (pin: string, choreId: number, body: ChoreChanges) =>
    apiFetch<Chore & { slug: string }>(`/api/chores/${choreId}`, {
      method: 'PATCH',
      headers: managerHeaders(pin, true),
      body: JSON.stringify(body),
    }),

  retireChore: (pin: string, choreId: number) =>
    apiFetch<void>(`/api/chores/${choreId}`, {
      method: 'DELETE',
      headers: managerHeaders(pin),
    }),

  award: (pin: string, slug: string, body: { points: number; description: string }) =>
    apiFetch<LedgerResult>(`/api/members/${enc(slug)}/award`, {
      method: 'POST',
      headers: managerHeaders(pin, true),
      body: JSON.stringify(body),
    }),

  deduct: (pin: string, slug: string, body: { points: number; description: string }) =>
    apiFetch<LedgerResult>(`/api/members/${enc(slug)}/deduct`, {
      method: 'POST',
      headers: managerHeaders(pin, true),
      body: JSON.stringify(body),
    }),

  getTransactions: (pin: string, slug?: string, limit?: number) =>
    apiFetch<Transaction[]>(`/api/transactions${query({ slug, limit })}`, {
      headers: managerHeaders(pin),
    }),
}

