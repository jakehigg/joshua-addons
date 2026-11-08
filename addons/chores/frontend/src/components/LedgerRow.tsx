import { Transaction, formatDateTime } from '../api'

// One history row, in a `.j-list`. Both views use it.
export default function LedgerRow({ tx }: { tx: Transaction }) {
  return (
    <li className="j-list-row">
      <div className="j-list-main">
        <span className="j-list-title wrap">{tx.description}</span>
        <span className="j-list-sub">{formatDateTime(tx.created_at)}</span>
      </div>
      <span className={`ledger-amount j-num ${tx.amount < 0 ? 'negative' : 'positive'}`}>
        {tx.amount > 0 ? '+' : ''}{tx.amount} XP
      </span>
    </li>
  )
}
