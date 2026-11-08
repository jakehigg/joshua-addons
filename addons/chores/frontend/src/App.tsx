import { BrowserRouter, Routes, Route } from 'react-router-dom'
import ManagerView from './components/ManagerView'
import MemberScreen from './components/MemberScreen'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<ManagerView />} />
        <Route path="/:slug" element={<MemberScreen />} />
      </Routes>
    </BrowserRouter>
  )
}
