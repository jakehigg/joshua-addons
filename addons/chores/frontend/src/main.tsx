import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// The shared Joshua look first, then the chores-only styles on top of it.
import './joshua-ui.css'
import './index.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
