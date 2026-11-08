import { useEffect, useState, useCallback, useRef } from 'react'
import { useParams } from 'react-router-dom'
import { api, ApiError, ChoreRow, Member, Transaction, formatDate } from '../api'
import Icon from './Icon'
import LedgerRow from './LedgerRow'

// The minimum time that the Done button stays off after a tap. The server's
// cooldown_seconds makes it longer when the server sends a larger value.
const CLIENT_COOLDOWN_MS = 5000
// How long a toast stays on the screen: a completion, and an error (for
// example a cooldown refusal from the server).
const TOAST_OK_MS = 1500
const TOAST_ERROR_MS = 3000
const FLASH_MS = 1200
const VERSION_POLL_MS = 60_000

// The time, by chore id, when the cooldown of the chore ends.
interface CooldownState {
  [choreId: number]: number
}

interface Toast {
  // A new id for each toast restarts the slide-in animation.
  id: number
  kind: 'ok' | 'error'
  text: string
}

export default function MemberScreen() {
  const { slug } = useParams<{ slug: string }>()
  const [member, setMember] = useState<Member | null>(null)
  const [chores, setChores] = useState<ChoreRow[]>([])
  const [today, setToday] = useState('')
  const [ledger, setLedger] = useState<Transaction[]>([])
  const [cooldownMs, setCooldownMs] = useState(CLIENT_COOLDOWN_MS)
  const [cooldowns, setCooldowns] = useState<CooldownState>({})
  const [flash, setFlash] = useState<number | null>(null)
  const [toast, setToast] = useState<Toast | null>(null)
  // Increments each time the balance changes. The balance element uses it as
  // its key, so that the bump animation starts again.
  const [bump, setBump] = useState(0)
  const [error, setError] = useState('')
  const [, setTick] = useState(0)
  const versionRef = useRef<string | null>(null)
  const toastTimer = useRef<number | undefined>(undefined)
  const toastId = useRef(0)

  const loadLists = useCallback(async (memberSlug: string) => {
    const [choreList, ledgerList] = await Promise.all([
      api.getChores(memberSlug),
      api.getLedger(memberSlug),
    ])
    setChores(choreList.chores)
    setToday(choreList.today)
    setLedger(ledgerList)
  }, [])

  const load = useCallback(async () => {
    if (!slug) return
    try {
      const me = await api.getMember(slug)
      setMember(me)
      await loadLists(slug)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setError(`Unknown screen: ${slug}`)
      else setError(e instanceof Error ? e.message : String(e))
    }
  }, [slug, loadLists])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    api.getSettings()
      .then(s => setCooldownMs(Math.max(CLIENT_COOLDOWN_MS, s.cooldown_seconds * 1000)))
      .catch(() => { /* Keep the client cooldown. */ })
  }, [])

  // Draw the cooldown timers again each second.
  useEffect(() => {
    const t = setInterval(() => setTick(n => n + 1), 1000)
    return () => clearInterval(t)
  }, [])

  // Poll the version. Reload the page when a deploy changes it.
  useEffect(() => {
    const check = async () => {
      try {
        const { version } = await api.getVersion()
        if (versionRef.current === null) { versionRef.current = version; return }
        if (version !== versionRef.current) window.location.reload()
      } catch { /* Try again at the next poll. */ }
    }
    check()
    const t = setInterval(check, VERSION_POLL_MS)
    return () => clearInterval(t)
  }, [])

  useEffect(() => () => window.clearTimeout(toastTimer.current), [])

  function showToast(kind: Toast['kind'], text: string) {
    window.clearTimeout(toastTimer.current)
    toastId.current += 1
    setToast({ id: toastId.current, kind, text })
    toastTimer.current = window.setTimeout(
      () => setToast(null),
      kind === 'ok' ? TOAST_OK_MS : TOAST_ERROR_MS,
    )
  }

  async function handleComplete(chore: ChoreRow) {
    if (inCooldown(chore.id) || !slug) return
    setCooldowns(prev => ({ ...prev, [chore.id]: Date.now() + CLIENT_COOLDOWN_MS }))

    try {
      const result = await api.completeChore(chore.id)
      if (result.balance !== member?.balance) setBump(n => n + 1)
      setMember(prev => prev && {
        ...prev, balance: result.balance, balance_dollars: result.balance_dollars,
      })
      showToast('ok', `+${result.points_awarded} XP · ${result.chore_name}`)
      setFlash(chore.id)
      setTimeout(() => setFlash(null), FLASH_MS)
      setCooldowns(prev => ({ ...prev, [chore.id]: Date.now() + cooldownMs }))
      await loadLists(slug)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.retryAfter !== null) {
        const until = Date.now() + e.retryAfter * 1000
        setCooldowns(prev => ({ ...prev, [chore.id]: until }))
      }
      showToast('error', e instanceof Error ? e.message : String(e))
    }
  }

  function remainingSeconds(choreId: number): number {
    const expiry = cooldowns[choreId]
    return expiry ? Math.ceil((expiry - Date.now()) / 1000) : 0
  }

  function inCooldown(choreId: number): boolean {
    return remainingSeconds(choreId) > 0
  }

  if (error) return <div className="kiosk kiosk-message bad">{error}</div>
  if (!member) return <div className="kiosk kiosk-message"><span className="j-spinner" />Loading…</div>

  return (
    <div className="kiosk">
      {toast && (
        <div
          key={toast.id}
          className={`kiosk-toast ${toast.kind}`}
          role={toast.kind === 'error' ? 'alert' : 'status'}
        >
          {toast.text}
        </div>
      )}

      <div className="kiosk-inner">
        <header className="kiosk-header">
          <div className="kiosk-brand">
            <img src="/logo-64.png" alt="" />
            <span>Joshua · Chores</span>
          </div>
          <h1 className="kiosk-name">{member.name}</h1>
          <div key={bump} className={`kiosk-balance j-num ${bump > 0 ? 'bump' : ''}`}>{member.balance} XP</div>
          {member.balance_dollars !== null && (
            <div className="kiosk-dollars j-num">{member.balance_dollars}</div>
          )}
        </header>

        <ul className="kiosk-list">
          {chores.length === 0 && (
            <li className="kiosk-empty">
              <Icon name="check" />All done! Great work.
            </li>
          )}
          {chores.map(chore => {
            const late = chore.next_due_date < today
            const dueToday = chore.next_due_date === today
            const wait = remainingSeconds(chore.id)
            const cooling = wait > 0

            return (
              <li
                key={chore.id}
                className={[
                  'kiosk-chore',
                  late ? 'overdue' : '',
                  flash === chore.id ? 'flash' : '',
                ].join(' ')}
              >
                <div className="kiosk-chore-info">
                  <span className="kiosk-chore-name">{chore.name}</span>
                  <div className="kiosk-chore-due">
                    {late && <span className="j-badge warn">Overdue</span>}
                    {dueToday && <span className="j-badge ok">Due today</span>}
                    <span className="j-muted">{formatDate(chore.next_due_date)}</span>
                  </div>
                </div>
                <div className="kiosk-chore-right">
                  <span className="kiosk-points j-num">+{chore.points}</span>
                  <button
                    className={`done-btn ${cooling ? 'cooldown' : ''}`}
                    onClick={() => handleComplete(chore)}
                    disabled={cooling}
                  >
                    {cooling ? `Wait ${wait} s` : <><Icon name="check" />Done</>}
                  </button>
                </div>
              </li>
            )
          })}
        </ul>

        {ledger.length > 0 && (
          <section className="j-stack kiosk-history" aria-label="History">
            <h2 className="j-label">History</h2>
            <ul className="j-list ledger-scroll" tabIndex={0}>
              {ledger.map(tx => <LedgerRow key={tx.id} tx={tx} />)}
            </ul>
          </section>
        )}
      </div>
    </div>
  )
}
