import { Transaction, formatDateTime } from '../api'

// One history row. The styles are in index.css, because both views use it.
export default function LedgerRow({ tx }: { tx: Transaction }) {
  return (
    <li className="ledger-row">
      <div className="ledger-left">
        <span className="ledger-desc">{tx.description}</span>
        <span className="ledger-date">{formatDateTime(tx.created_at)}</span>
      </div>
      <span className={`ledger-amount ${tx.amount < 0 ? 'negative' : 'positive'}`}>
        {tx.amount > 0 ? '+' : ''}{tx.amount} XP
      </span>
    </li>
  )
}
