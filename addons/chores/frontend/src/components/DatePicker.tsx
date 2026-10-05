import { useState } from 'react'
import { localToday } from '../api'
import './DatePicker.css'

interface Props {
  value: string // YYYY-MM-DD
  onChange: (value: string) => void
}

const DOW = ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa']
const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

function toISO(y: number, m: number, d: number): string {
  return `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`
}

export default function DatePicker({ value, onChange }: Props) {
  const initial = value ? new Date(value + 'T00:00:00') : new Date()
  const [viewYear, setViewYear] = useState(initial.getFullYear())
  const [viewMonth, setViewMonth] = useState(initial.getMonth())

  const todayStr = localToday()

  function prev() {
    if (viewMonth === 0) { setViewMonth(11); setViewYear(y => y - 1) }
    else setViewMonth(m => m - 1)
  }

  function next() {
    if (viewMonth === 11) { setViewMonth(0); setViewYear(y => y + 1) }
    else setViewMonth(m => m + 1)
  }

  const firstDow = new Date(viewYear, viewMonth, 1).getDay()
  const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate()
  const cells: (number | null)[] = [
    ...Array(firstDow).fill(null),
    ...Array.from({ length: daysInMonth }, (_, i) => i + 1),
  ]

  return (
    <div className="datepicker">
      <div className="dp-header">
        <button type="button" className="dp-nav" onClick={prev}>‹</button>
        <span className="dp-month">{MONTHS[viewMonth]} {viewYear}</span>
        <button type="button" className="dp-nav" onClick={next}>›</button>
      </div>
      <div className="dp-dow">
        {DOW.map(d => <span key={d}>{d}</span>)}
      </div>
      <div className="dp-grid">
        {cells.map((day, i) => {
          if (!day) return <span key={`_${i}`} />
          const iso = toISO(viewYear, viewMonth, day)
          return (
            <button
              key={iso}
              type="button"
              className={[
                'dp-day',
                iso === value ? 'selected' : '',
                iso === todayStr ? 'today' : '',
              ].filter(Boolean).join(' ')}
              onClick={() => onChange(iso)}
            >
              {day}
            </button>
          )
        })}
      </div>
    </div>
  )
}
