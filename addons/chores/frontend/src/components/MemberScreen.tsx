import { useEffect, useState, useCallback, useRef } from 'react'
import { useParams } from 'react-router-dom'
import { api, ApiError, ChoreRow, Member, Transaction, formatDate, formatDateTime } from '../api'
import './MemberScreen.css'

// The minimum time that the Done button stays off after a tap. The server's
// cooldown_seconds makes it longer when the server sends a larger value.
const CLIENT_COOLDOWN_MS = 5000
// How long a server message (for example a cooldown refusal) stays on a chore.
const NOTICE_MS = 3000
const VERSION_POLL_MS = 60_000

// The time, by chore id, when the cooldown of the chore ends.
interface CooldownState {
  [choreId: number]: number
}

interface Notice {
  choreId: number
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
  const [notice, setNotice] = useState<Notice | null>(null)
  const [error, setError] = useState('')
  const [, setTick] = useState(0)
  const versionRef = useRef<string | null>(null)
  const noticeTimer = useRef<number | undefined>(undefined)

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

  useEffect(() => () => window.clearTimeout(noticeTimer.current), [])

  function showNotice(choreId: number, text: string) {
    window.clearTimeout(noticeTimer.current)
    setNotice({ choreId, text })
    noticeTimer.current = window.setTimeout(() => setNotice(null), NOTICE_MS)
  }

  async function handleComplete(chore: ChoreRow) {
    if (inCooldown(chore.id) || !slug) return
    setCooldowns(prev => ({ ...prev, [chore.id]: Date.now() + CLIENT_COOLDOWN_MS }))

    try {
      const result = await api.completeChore(chore.id)
      setMember(prev => prev && {
        ...prev, balance: result.balance, balance_dollars: result.balance_dollars,
      })
      setFlash(chore.id)
      setTimeout(() => setFlash(null), 1200)
      setCooldowns(prev => ({ ...prev, [chore.id]: Date.now() + cooldownMs }))
      await loadLists(slug)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.retryAfter !== null) {
        const until = Date.now() + e.retryAfter * 1000
        setCooldowns(prev => ({ ...prev, [chore.id]: until }))
      }
      showNotice(chore.id, e instanceof Error ? e.message : String(e))
    }
  }

  function remainingSeconds(choreId: number): number {
    const expiry = cooldowns[choreId]
    return expiry ? Math.ceil((expiry - Date.now()) / 1000) : 0
  }

  function inCooldown(choreId: number): boolean {
    return remainingSeconds(choreId) > 0
  }

  if (error) return <div className="kiosk screen-error">{error}</div>
  if (!member) return <div className="kiosk screen-loading">Loading...</div>

  return (
    <div className="kiosk screen-root">
      <header className="screen-header">
        <h1 className="screen-name">{member.name}</h1>
        <div className="screen-balance">{member.balance} XP</div>
        {member.balance_dollars !== null && (
          <div className="screen-dollars">{member.balance_dollars}</div>
        )}
      </header>

      <ul className="screen-list">
        {chores.length === 0 && (
          <li className="screen-empty">All done! Great work.</li>
        )}
        {chores.map(chore => {
          const overdue = chore.overdue
          const dueLabel = chore.next_due_date === today ? 'Due today · ' : 'Overdue · '
          const wait = remainingSeconds(chore.id)
          const cooling = wait > 0
          const isFlashing = flash === chore.id
          const message = notice?.choreId === chore.id ? notice.text : null

          return (
            <li
              key={chore.id}
              className={[
                'screen-chore',
                overdue ? 'overdue' : '',
                isFlashing ? 'flash' : '',
              ].join(' ')}
            >
              <div className="chore-info">
                <span className="chore-name">{chore.name}</span>
                {message ? (
                  <span className="chore-due overdue-text" role="status">{message}</span>
                ) : (
                  <span className={`chore-due ${overdue ? 'overdue-text' : ''}`}>
                    {overdue ? dueLabel : ''}{formatDate(chore.next_due_date)}
                  </span>
                )}
              </div>
              <div className="chore-right">
                <span className="chore-pts">+{chore.points}</span>
                <button
                  className={`done-btn ${cooling ? 'cooldown' : ''}`}
                  onClick={() => handleComplete(chore)}
                  disabled={cooling}
                >
                  {cooling && wait > CLIENT_COOLDOWN_MS / 1000 ? `${wait}s` : 'Done'}
                </button>
              </div>
            </li>
          )
        })}
      </ul>

      {ledger.length > 0 && (
        <section className="screen-ledger">
          <h2 className="ledger-title">History</h2>
          <ul className="ledger-list">
            {ledger.map(tx => (
              <li key={tx.id} className="ledger-row">
                <span className="ledger-desc">{tx.description}</span>
                <span className="ledger-date">{formatDateTime(tx.created_at)}</span>
                <span className={`ledger-amount ${tx.amount < 0 ? 'negative' : 'positive'}`}>
                  {tx.amount > 0 ? '+' : ''}{tx.amount} XP
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
