import { useCallback, useEffect, useRef, useState } from 'react'
import {
  api, ApiError, Chore, ChoreRow, FREQUENCY_LABEL, Frequency, Member, Transaction,
  formatDate, localToday,
} from '../api'
import DatePicker from './DatePicker'
import Icon from './Icon'
import LedgerRow from './LedgerRow'

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
      <div className="pin-screen">
        <form onSubmit={handlePinSubmit} className="j-card pin-card">
          <img className="pin-logo" src="/logo-64.png" alt="" />
          <div className="pin-heading">
            <small>Joshua</small>
            <h1>Chores</h1>
          </div>
          <p className="j-muted pin-sub">{label} sign in</p>
          <label className={`j-field ${pinError ? 'invalid' : ''}`}>
            <input
              type="password"
              inputMode="numeric"
              placeholder="PIN"
              aria-label="PIN"
              value={pinInput}
              onChange={e => setPinInput(e.target.value)}
              autoFocus
            />
          </label>
          {pinError && <p className="j-error" role="alert">{pinError}</p>}
          <button type="submit" className="j-btn primary block">Unlock</button>
        </form>
      </div>
    )
  }

  const dayToday = today || localToday()
  const overdueCount = chores.filter(c => c.next_due_date < dayToday).length
  const dueTodayCount = chores.filter(c => c.next_due_date === dayToday).length

  return (
    <div className="manager">
      <header className="j-appbar">
        <img className="j-logo" src="/logo-64.png" alt="" />
        <a className="j-appbar-title" href="/">
          <small>Joshua</small>
          <strong>Chores</strong>
        </a>
        <span className="j-spacer" />
        <button type="button" className="j-icon-btn" onClick={signOut} aria-label="Sign out" title="Sign out">
          <Icon name="signout" />
        </button>
      </header>

      <nav className="j-tabs" role="tablist" aria-label="Chores sections">
        <button
          type="button"
          role="tab"
          className="j-tab"
          aria-selected={tab === 'chores'}
          onClick={() => setTab('chores')}
        >
          Chores
        </button>
        <button
          type="button"
          role="tab"
          className="j-tab"
          aria-selected={tab === 'members'}
          onClick={() => setTab('members')}
        >
          Members
        </button>
      </nav>

      <main className="j-content">
        {error && (
          <div className="alert" role="alert">
            <span>{error}</span>
            <button type="button" className="j-icon-btn sm" onClick={() => setError('')} aria-label="Dismiss">
              <Icon name="close" />
            </button>
          </div>
        )}

        {tab === 'chores' && (
          <>
            {activeMembers.length > 0 && (
              <div className="j-chips" role="group" aria-label="Member">
                {activeMembers.map(m => (
                  <button
                    key={m.slug}
                    type="button"
                    className="j-chip"
                    aria-pressed={m.slug === selectedSlug}
                    onClick={() => setSelectedSlug(m.slug)}
                  >
                    {m.name}
                  </button>
                ))}
              </div>
            )}

            {selected && (
              <div className="j-stats">
                <div className="j-stat ok">
                  <b>{selected.balance}</b>
                  <span>XP</span>
                </div>
                {selected.balance_dollars !== null && (
                  <div className="j-stat">
                    <b>{selected.balance_dollars}</b>
                    <span>Balance</span>
                  </div>
                )}
                <div className="j-stat">
                  <b>{dueTodayCount}</b>
                  <span>Due today</span>
                </div>
                <div className={`j-stat ${overdueCount > 0 ? 'warn' : ''}`}>
                  <b>{overdueCount}</b>
                  <span>Overdue</span>
                </div>
              </div>
            )}

            <div className="j-row-flex action-row">
              <button className="j-btn primary" onClick={() => setModal({ type: 'add' })} disabled={!selected}>
                <Icon name="plus" /><span className="label-long">Add chore</span><span className="label-short">Add</span>
              </button>
              <button className="j-btn" onClick={() => setModal({ type: 'award' })} disabled={!selected}>
                <Icon name="plus" /><span className="label-long">Award XP</span><span className="label-short">Award</span>
              </button>
              <button className="j-btn danger" onClick={() => setModal({ type: 'deduct' })} disabled={!selected}>
                <Icon name="minus" /><span className="label-long">Deduct XP</span><span className="label-short">Deduct</span>
              </button>
            </div>

            {members.length === 0 && <div className="j-empty">Add a member first</div>}
            {members.length > 0 && !selected && <div className="j-empty">No active members</div>}
            {selected && chores.length === 0 && <div className="j-empty">No chores yet</div>}
            {chores.length > 0 && (
              <ul className="j-list">
                {chores.map(chore => {
                  const late = chore.next_due_date < dayToday
                  const dueToday = chore.next_due_date === dayToday
                  return (
                    <li key={chore.id} className="j-list-row">
                      <span className={`j-dot ${late ? 'warn' : dueToday ? 'ok' : ''}`} aria-hidden="true" />
                      <div className="j-list-main">
                        <div className="title-line">
                          <span className="j-list-title wrap">{chore.name}</span>
                          {late && <span className="j-badge warn">Overdue</span>}
                          {dueToday && <span className="j-badge ok">Due today</span>}
                        </div>
                        <span className="j-list-sub">
                          {FREQUENCY_LABEL[chore.frequency]} · {chore.points} XP · {formatDate(chore.next_due_date)}
                        </span>
                      </div>
                      <div className="row-actions">
                        <button className="j-icon-btn sm" onClick={() => setModal({ type: 'edit', chore })} title="Edit" aria-label={`Edit ${chore.name}`}>
                          <Icon name="edit" />
                        </button>
                        <button className="j-icon-btn sm act-danger" onClick={() => handleRetire(chore)} title="Delete" aria-label={`Delete ${chore.name}`}>
                          <Icon name="trash" />
                        </button>
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}

            {ledger.length > 0 && (
              <section className="j-stack history">
                <h2 className="j-label">History</h2>
                <ul className="j-list">
                  {ledger.map(tx => <LedgerRow key={tx.id} tx={tx} />)}
                </ul>
              </section>
            )}
          </>
        )}

        {tab === 'members' && (
          <>
            <div className="j-row-flex">
              <button className="j-btn primary" onClick={() => setModal({ type: 'add-member' })}>
                <Icon name="plus" />Add member
              </button>
            </div>
            {members.length === 0 && <div className="j-empty">No members yet</div>}
            {members.length > 0 && (
              <ul className="j-list">
                {members.map((member, index) => (
                  <li key={member.slug} className={`j-list-row ${member.is_active ? '' : 'inactive'}`}>
                    <div className="j-list-main">
                      <div className="title-line">
                        <span className="j-list-title">{member.name}</span>
                        {!member.is_active && <span className="j-badge plain">Inactive</span>}
                      </div>
                      <span className="j-list-sub j-num">{member.balance} XP · /{member.slug}</span>
                    </div>
                    <div className="row-actions">
                      <button className="j-icon-btn sm" onClick={() => move(index, -1)} disabled={index === 0} title="Move up" aria-label={`Move ${member.name} up`}>
                        <Icon name="up" />
                      </button>
                      <button className="j-icon-btn sm" onClick={() => move(index, 1)} disabled={index === members.length - 1} title="Move down" aria-label={`Move ${member.name} down`}>
                        <Icon name="down" />
                      </button>
                      <button className="j-icon-btn sm" onClick={() => setModal({ type: 'rename', member })} title="Rename" aria-label={`Rename ${member.name}`}>
                        <Icon name="edit" />
                      </button>
                      <input
                        type="checkbox"
                        className="j-toggle"
                        checked={member.is_active}
                        onChange={() => toggleActive(member)}
                        title={member.is_active ? 'Active' : 'Inactive'}
                        aria-label={`${member.name} active`}
                      />
                    </div>
                  </li>
                ))}
              </ul>
            )}
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
    <div className="dialog-scrim" onClick={onClose} ref={overlayRef}>
      <div
        className="j-card dialog"
        role="dialog"
        aria-modal="true"
        onClick={e => e.stopPropagation()}
        onFocus={handleFocus}
      >
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
    <form onSubmit={handleSubmit} className="dialog-form">
      <h2>{chore ? 'Edit chore' : 'Add chore'}</h2>
      {err && <p className="j-error" role="alert">{err}</p>}
      <label className="j-field-label">
        Name
        <span className="j-field">
          <input value={name} onChange={e => setName(e.target.value)} required autoFocus />
        </span>
      </label>
      <label className="j-field-label">
        XP
        <span className="j-field">
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
        </span>
      </label>
      <label className="j-field-label">
        Frequency
        <span className="j-field">
          <select value={frequency} onChange={e => setFrequency(e.target.value as Frequency)}>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="monthly">Monthly</option>
            <option value="one_off">One-off</option>
          </select>
        </span>
      </label>
      <div className="j-field-label">
        <span>Due date</span>
        <DatePicker value={dueDate} onChange={setDueDate} />
      </div>
      <div className="dialog-actions">
        <button type="button" className="j-btn ghost" onClick={onClose}>Cancel</button>
        <button type="submit" className="j-btn primary" disabled={saving || !valid}>{saving ? 'Saving…' : 'Save'}</button>
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
    <form onSubmit={handleSubmit} className="dialog-form">
      <h2>{title}</h2>
      {err && <p className="j-error" role="alert">{err}</p>}
      <label className="j-field-label">
        XP
        <span className="j-field">
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
        </span>
      </label>
      <label className="j-field-label">
        Description
        <span className="j-field">
          <input value={description} onChange={e => setDescription(e.target.value)} placeholder="e.g. cleaned the yard" required />
        </span>
      </label>
      <div className="dialog-actions">
        <button type="button" className="j-btn ghost" onClick={onClose}>Cancel</button>
        <button type="submit" className="j-btn primary" disabled={saving || !valid}>{saving ? 'Saving…' : 'Save'}</button>
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
    <form onSubmit={handleSubmit} className="dialog-form">
      <h2>{member ? 'Rename member' : 'Add member'}</h2>
      {err && <p className="j-error" role="alert">{err}</p>}
      {!member && (
        <label className="j-field-label">
          Slug
          <span className="j-field">
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
          </span>
          <span className={`field-hint ${slug && !slugValid ? 'invalid' : ''}`}>
            1 to 32 lowercase letters, digits, or hyphens. The kiosk URL is /{slug || 'slug'}. You cannot change the slug later.
          </span>
        </label>
      )}
      <label className="j-field-label">
        Name
        <span className="j-field">
          <input value={name} onChange={e => setName(e.target.value)} required autoFocus={!!member} />
        </span>
      </label>
      <div className="dialog-actions">
        <button type="button" className="j-btn ghost" onClick={onClose}>Cancel</button>
        <button type="submit" className="j-btn primary" disabled={saving || !valid}>{saving ? 'Saving…' : 'Save'}</button>
      </div>
    </form>
  )
}
