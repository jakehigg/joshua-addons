import { useCallback, useEffect, useRef, useState } from 'react'
import {
  api, ApiError, Chore, ChoreRow, FREQUENCY_LABEL, Frequency, Member, Transaction,
  formatDate, localToday,
} from '../api'
import DatePicker from './DatePicker'
import LedgerRow from './LedgerRow'
import './ManagerView.css'

const PIN_KEY = 'chores_manager_pin'
const SLUG_PATTERN = /^[a-z0-9-]{1,32}$/
const XP_PATTERN = /^[1-9]\d*$/
const NO_PIN_MESSAGE = 'The server has no manager PIN set'

type Tab = 'chores' | 'members'

type Modal =
  | { type: 'add' }
  | { type: 'edit'; chore: ChoreRow }
  | { type: 'award' }
  | { type: 'deduct' }
  | { type: 'add-member' }
  | { type: 'rename'; member: Member }
  | null

function errorText(e: unknown): string {
  if (e instanceof ApiError && e.status === 503) return NO_PIN_MESSAGE
  return e instanceof Error ? e.message : String(e)
}

function readPin(): string {
  try {
    return sessionStorage.getItem(PIN_KEY) || ''
  } catch {
    return ''
  }
}

function writePin(pin: string) {
  try {
    if (pin) sessionStorage.setItem(PIN_KEY, pin)
    else sessionStorage.removeItem(PIN_KEY)
  } catch {
    // The browser blocks storage. The PIN stays for this page load only.
  }
}

export default function ManagerView() {
  const [label, setLabel] = useState('Manager')
  const [pin, setPin] = useState(readPin)
  const [pinInput, setPinInput] = useState('')
  const [pinError, setPinError] = useState('')
  const [tab, setTab] = useState<Tab>('chores')
  const [members, setMembers] = useState<Member[]>([])
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null)
  const [chores, setChores] = useState<ChoreRow[]>([])
  const [today, setToday] = useState('')
  const [ledger, setLedger] = useState<Transaction[]>([])
  const [modal, setModal] = useState<Modal>(null)
  const [error, setError] = useState('')

  const authed = !!pin
  const activeMembers = members.filter(m => m.is_active)
  const selected = members.find(m => m.slug === selectedSlug) ?? null

  useEffect(() => {
    api.getSettings().then(s => setLabel(s.manager_label)).catch(() => { /* Keep the default label. */ })
  }, [])

  const signOut = useCallback(() => {
    writePin('')
    setPin('')
  }, [])

  // Show an error. A 401 means the stored PIN is wrong now, so sign out.
  const fail = useCallback((e: unknown) => {
    if (e instanceof ApiError && e.status === 401) {
      signOut()
      setPinError('Incorrect PIN')
      return
    }
    setError(errorText(e))
  }, [signOut])

  // Load the members again. Keep the selected member if it is still active.
  const refreshMembers = useCallback(async () => {
    const list = await api.getMembers(true)
    setMembers(list)
    setSelectedSlug(prev => {
      const active = list.filter(m => m.is_active)
      if (prev && active.some(m => m.slug === prev)) return prev
      return active.length > 0 ? active[0].slug : null
    })
  }, [])

  const refreshMemberData = useCallback(async (slug: string) => {
    const [choreList, txList] = await Promise.all([
      api.getChores(slug),
      api.getTransactions(pin, slug),
    ])
    setChores(choreList.chores)
    setToday(choreList.today)
    setLedger(txList)
  }, [pin])

  useEffect(() => {
    if (!authed) return
    refreshMembers().catch(fail)
  }, [authed, refreshMembers, fail])

  useEffect(() => {
    if (!authed || !selectedSlug) {
      setChores([])
      setLedger([])
      return
    }
    refreshMemberData(selectedSlug).catch(fail)
  }, [authed, selectedSlug, refreshMemberData, fail])

  async function handlePinSubmit(e: React.FormEvent) {
    e.preventDefault()
    try {
      await api.getTransactions(pinInput, undefined, 1)
      writePin(pinInput)
      setPin(pinInput)
      setPinError('')
      setError('')
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) setPinError('Incorrect PIN')
      else setPinError(errorText(err))
    }
  }

  async function handleRetire(chore: ChoreRow) {
    if (!confirm(`Delete "${chore.name}"?`)) return
    try {
      await api.retireChore(pin, chore.id)
      setChores(prev => prev.filter(c => c.id !== chore.id))
    } catch (e) {
      fail(e)
    }
  }

  async function afterChange() {
    setModal(null)
    setError('')
    try {
      await refreshMembers()
      if (selectedSlug) await refreshMemberData(selectedSlug)
    } catch (e) {
      fail(e)
    }
  }

  async function memberWrite(write: () => Promise<unknown>) {
    try {
      await write()
      await afterChange()
    } catch (e) {
      fail(e)
    }
  }

  function toggleActive(member: Member) {
    memberWrite(() => api.updateMember(pin, member.slug, { is_active: !member.is_active }))
  }

  // Move a member one place up or down. Write a sort_order for each member
  // whose place changes, so that members with the same sort_order also move.
  function move(index: number, step: -1 | 1) {
    const target = index + step
    if (target < 0 || target >= members.length) return
    const order = [...members]
    ;[order[index], order[target]] = [order[target], order[index]]
    memberWrite(async () => {
      for (const [position, member] of order.entries()) {
        if (member.sort_order !== position) {
          await api.updateMember(pin, member.slug, { sort_order: position })
        }
      }
    })
  }

  if (!authed) {
    return (
      <div className="manager-pin-screen">
        <h1>Chores</h1>
        <p className="pin-sub">{label} sign in</p>
        <form onSubmit={handlePinSubmit} className="pin-form">
          <input
            type="password"
            inputMode="numeric"
            placeholder="Enter PIN"
            value={pinInput}
            onChange={e => setPinInput(e.target.value)}
            autoFocus
          />
          {pinError && <div className="pin-error">{pinError}</div>}
          <button type="submit" className="btn-primary">Unlock</button>
        </form>
      </div>
    )
  }

  const dayToday = today || localToday()

  return (
    <div className="manager-root">
      <header className="manager-header">
        <div className="manager-inner">
          <h1>Chores</h1>
          {tab === 'chores' && activeMembers.length > 0 && (
            <div className="member-chips" aria-label="Member">
              {activeMembers.map(m => (
                <button
                  key={m.slug}
                  type="button"
                  className={`member-chip ${m.slug === selectedSlug ? 'active' : ''}`}
                  aria-pressed={m.slug === selectedSlug}
                  onClick={() => setSelectedSlug(m.slug)}
                >
                  {m.name}
                </button>
              ))}
            </div>
          )}
          {tab === 'chores' && selected && (
            <div className="balance-bar">
              {selected.balance_dollars !== null ? (
                <>
                  <span className="balance-dollars">{selected.balance_dollars}</span>
                  <span className="balance-pts">{selected.balance} XP</span>
                </>
              ) : (
                <span className="balance-dollars">{selected.balance} XP</span>
              )}
            </div>
          )}
        </div>
      </header>

      <main className="manager-inner manager-main">
        <div className="view-switch" role="tablist">
          <button
            role="tab"
            aria-selected={tab === 'chores'}
            className={tab === 'chores' ? 'active' : ''}
            onClick={() => setTab('chores')}
          >
            Chores
          </button>
          <button
            role="tab"
            aria-selected={tab === 'members'}
            className={tab === 'members' ? 'active' : ''}
            onClick={() => setTab('members')}
          >
            Members
          </button>
        </div>

        {error && <div className="manager-error">{error}</div>}

        {tab === 'chores' && (
          <>
            <div className="action-row">
              <button className="btn-primary" onClick={() => setModal({ type: 'add' })} disabled={!selected}>
                <span className="label-long">+ Add chore</span>
                <span className="label-short">Add</span>
              </button>
              <button className="btn-secondary" onClick={() => setModal({ type: 'award' })} disabled={!selected}>
                <span className="label-long">Award XP</span>
                <span className="label-short">Award</span>
              </button>
              <button className="btn-danger" onClick={() => setModal({ type: 'deduct' })} disabled={!selected}>
                <span className="label-long">Deduct XP</span>
                <span className="label-short">Deduct</span>
              </button>
            </div>

            <ul className="manager-list">
              {members.length === 0 && <li className="empty-msg">Add a member first</li>}
              {members.length > 0 && !selected && <li className="empty-msg">No active members</li>}
              {selected && chores.length === 0 && <li className="empty-msg">No chores yet</li>}
              {chores.map(chore => {
                const late = chore.next_due_date < dayToday
                const dueToday = chore.next_due_date === dayToday
                return (
                  <li key={chore.id} className={`manager-chore ${late ? 'overdue' : ''}`}>
                    <div className="chore-meta">
                      <span className="chore-name">{chore.name}</span>
                      <div className="chore-line">
                        {late && <span className="chip chip-amber">Overdue</span>}
                        {dueToday && <span className="chip chip-green">Due today</span>}
                        <span className="chore-sub">
                          {FREQUENCY_LABEL[chore.frequency]} · {chore.points} XP · {formatDate(chore.next_due_date)}
                        </span>
                      </div>
                    </div>
                    <div className="chore-actions">
                      <button className="icon-btn" onClick={() => setModal({ type: 'edit', chore })} title="Edit" aria-label="Edit">✎</button>
                      <button className="icon-btn danger" onClick={() => handleRetire(chore)} title="Delete" aria-label="Delete">✕</button>
                    </div>
                  </li>
                )
              })}
            </ul>

            {ledger.length > 0 && (
              <section className="manager-ledger">
                <h2 className="ledger-title">History</h2>
                <ul className="ledger-list">
                  {ledger.map(tx => <LedgerRow key={tx.id} tx={tx} />)}
                </ul>
              </section>
            )}
          </>
        )}

        {tab === 'members' && (
          <>
            <div className="action-row">
              <button className="btn-primary" onClick={() => setModal({ type: 'add-member' })}>+ Add member</button>
            </div>
            <ul className="manager-list">
              {members.length === 0 && <li className="empty-msg">No members yet</li>}
              {members.map((member, index) => (
                <li key={member.slug} className={`manager-chore ${member.is_active ? '' : 'inactive'}`}>
                  <div className="chore-meta">
                    <div className="chore-line">
                      <span className="chore-name">{member.name}</span>
                      {!member.is_active && <span className="chip chip-muted">Inactive</span>}
                    </div>
                    <span className="chore-sub member-sub">{member.balance} XP · /{member.slug}</span>
                  </div>
                  <div className="chore-actions">
                    <button className="icon-btn" onClick={() => move(index, -1)} disabled={index === 0} title="Move up" aria-label="Move up">↑</button>
                    <button className="icon-btn" onClick={() => move(index, 1)} disabled={index === members.length - 1} title="Move down" aria-label="Move down">↓</button>
                    <button className="icon-btn" onClick={() => setModal({ type: 'rename', member })} title="Rename" aria-label="Rename">✎</button>
                    <button className="btn-secondary toggle-btn" onClick={() => toggleActive(member)}>
                      {member.is_active ? 'Deactivate' : 'Activate'}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </>
        )}
      </main>

      {modal && (
        <ModalOverlay onClose={() => setModal(null)}>
          {modal.type === 'add' && selected && (
            <ChoreForm
              slug={selected.slug}
              pin={pin}
              onSave={afterChange}
              onClose={() => setModal(null)}
            />
          )}
          {modal.type === 'edit' && selected && (
            <ChoreForm
              slug={selected.slug}
              pin={pin}
              chore={modal.chore}
              onSave={afterChange}
              onClose={() => setModal(null)}
            />
          )}
          {modal.type === 'award' && selected && (
            <XpForm
              title="Award XP"
              slug={selected.slug}
              pin={pin}
              mode="award"
              onSave={afterChange}
              onClose={() => setModal(null)}
            />
          )}
          {modal.type === 'deduct' && selected && (
            <XpForm
              title="Deduct XP"
              slug={selected.slug}
              pin={pin}
              mode="deduct"
              onSave={afterChange}
              onClose={() => setModal(null)}
            />
          )}
          {modal.type === 'add-member' && (
            <MemberForm pin={pin} onSave={afterChange} onClose={() => setModal(null)} />
          )}
          {modal.type === 'rename' && (
            <MemberForm pin={pin} member={modal.member} onSave={afterChange} onClose={() => setModal(null)} />
          )}
        </ModalOverlay>
      )}
    </div>
  )
}

function ModalOverlay({ children, onClose }: { children: React.ReactNode; onClose: () => void }) {
  const overlayRef = useRef<HTMLDivElement>(null)

  // On a phone, the software keyboard makes the visual viewport smaller, but
  // not the layout viewport. A `position: fixed` overlay then stays at full
  // height, and the keyboard hides its bottom box. Attach the overlay to the
  // visual viewport, so that the box stays above the keyboard.
  useEffect(() => {
    const vv = window.visualViewport
    if (!vv) return
    const el = overlayRef.current
    if (!el) return

    const applyViewport = () => {
      el.style.height = `${vv.height}px`
      el.style.transform = `translateY(${vv.offsetTop}px)`
    }

    applyViewport()
    vv.addEventListener('resize', applyViewport)
    vv.addEventListener('scroll', applyViewport)
    return () => {
      vv.removeEventListener('resize', applyViewport)
      vv.removeEventListener('scroll', applyViewport)
    }
  }, [])

  // A focused input opens the keyboard. After the viewport changes size,
  // scroll the field into view, so that it stays visible during typing.
  function handleFocus(e: React.FocusEvent<HTMLDivElement>) {
    const target = e.target as HTMLElement
    setTimeout(() => target.scrollIntoView({ block: 'center', behavior: 'smooth' }), 100)
  }

  return (
    <div className="modal-overlay" onClick={onClose} ref={overlayRef}>
      <div className="modal-box" onClick={e => e.stopPropagation()} onFocus={handleFocus}>
        {children}
      </div>
    </div>
  )
}

function ChoreForm({
  slug, pin, chore, onSave, onClose,
}: {
  slug: string; pin: string; chore?: Chore
  onSave: () => void; onClose: () => void
}) {
  const [name, setName] = useState(chore?.name ?? '')
  // The XP field keeps the raw text. A number state changes an empty field
  // to 0, and the next digit then comes after the 0 ("05").
  const [points, setPoints] = useState<string>(chore ? String(chore.points) : '')
  const [frequency, setFrequency] = useState<Frequency>(chore?.frequency ?? 'weekly')
  const [dueDate, setDueDate] = useState(chore?.next_due_date ?? localToday())
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState('')

  const valid = XP_PATTERN.test(points)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!valid) return
    const xp = Number.parseInt(points, 10)
    setSaving(true)
    try {
      if (chore) {
        await api.updateChore(pin, chore.id, { name, points: xp, frequency, next_due_date: dueDate })
      } else {
        await api.createChore(pin, { slug, name, points: xp, frequency, next_due_date: dueDate })
      }
      onSave()
    } catch (e) {
      setErr(errorText(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="modal-form">
      <h2>{chore ? 'Edit Chore' : 'Add Chore'}</h2>
      {err && <div className="form-error">{err}</div>}
      <label>
        Name
        <input value={name} onChange={e => setName(e.target.value)} required autoFocus />
      </label>
      <label>
        XP
        <input
          type="number"
          inputMode="numeric"
          min={1}
          step={1}
          placeholder="XP"
          value={points}
          onChange={e => setPoints(e.target.value)}
          required
        />
      </label>
      <label>
        Frequency
        <select value={frequency} onChange={e => setFrequency(e.target.value as Frequency)}>
          <option value="daily">Daily</option>
          <option value="weekly">Weekly</option>
          <option value="monthly">Monthly</option>
          <option value="one_off">One-off</option>
        </select>
      </label>
      <div className="date-field">
        <span>Due Date</span>
        <DatePicker value={dueDate} onChange={setDueDate} />
      </div>
      <div className="form-actions">
        <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
        <button type="submit" className="btn-primary" disabled={saving || !valid}>{saving ? 'Saving…' : 'Save'}</button>
      </div>
    </form>
  )
}

function XpForm({
  title, slug, pin, mode, onSave, onClose,
}: {
  title: string; slug: string; pin: string; mode: 'award' | 'deduct'
  onSave: () => void; onClose: () => void
}) {
  // The XP field keeps the raw text. A number state changes an empty field
  // to 0, and the next digit then comes after the 0 ("05").
  const [points, setPoints] = useState<string>('')
  const [description, setDescription] = useState('')
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState('')

  const valid = XP_PATTERN.test(points)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!valid) return
    setSaving(true)
    try {
      const write = mode === 'award' ? api.award : api.deduct
      await write(pin, slug, { points: Number.parseInt(points, 10), description })
      onSave()
    } catch (e) {
      setErr(errorText(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="modal-form">
      <h2>{title}</h2>
      {err && <div className="form-error">{err}</div>}
      <label>
        XP
        <input
          type="number"
          inputMode="numeric"
          min={1}
          step={1}
          placeholder="XP"
          value={points}
          onChange={e => setPoints(e.target.value)}
          required
          autoFocus
        />
      </label>
      <label>
        Description
        <input value={description} onChange={e => setDescription(e.target.value)} placeholder="e.g. cleaned the yard" required />
      </label>
      <div className="form-actions">
        <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
        <button type="submit" className="btn-primary" disabled={saving || !valid}>{saving ? 'Saving…' : 'Save'}</button>
      </div>
    </form>
  )
}

function MemberForm({
  pin, member, onSave, onClose,
}: {
  pin: string; member?: Member
  onSave: () => void; onClose: () => void
}) {
  const [slug, setSlug] = useState(member?.slug ?? '')
  const [name, setName] = useState(member?.name ?? '')
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState('')

  const slugValid = !!member || SLUG_PATTERN.test(slug)
  const valid = slugValid && name.trim() !== ''

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!valid) return
    setSaving(true)
    try {
      if (member) await api.updateMember(pin, member.slug, { name: name.trim() })
      else await api.createMember(pin, { slug, name: name.trim() })
      onSave()
    } catch (e) {
      setErr(errorText(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="modal-form">
      <h2>{member ? 'Rename Member' : 'Add Member'}</h2>
      {err && <div className="form-error">{err}</div>}
      {!member && (
        <label>
          Slug
          <input
            value={slug}
            onChange={e => setSlug(e.target.value)}
            placeholder="slug"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            required
            autoFocus
          />
          <span className={`field-hint ${slug && !slugValid ? 'invalid' : ''}`}>
            1 to 32 lowercase letters, digits, or hyphens. The kiosk URL is /{slug || 'slug'}. You cannot change the slug later.
          </span>
        </label>
      )}
      <label>
        Name
        <input value={name} onChange={e => setName(e.target.value)} required autoFocus={!!member} />
      </label>
      <div className="form-actions">
        <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
        <button type="submit" className="btn-primary" disabled={saving || !valid}>{saving ? 'Saving…' : 'Save'}</button>
      </div>
    </form>
  )
}
