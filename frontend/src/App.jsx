import { Routes, Route, NavLink } from 'react-router-dom'
import Upload from './pages/Upload.jsx'
import Overview from './pages/Overview.jsx'
import Signals from './pages/Signals.jsx'
import IncidentDetail from './pages/IncidentDetail.jsx'
import Actions from './pages/Actions.jsx'

export default function App() {
  return (
    <div className="layout">
      <nav className="sidebar">
        <h1>Signal Sprint</h1>
        <NavLink to="/">Upload</NavLink>
        <NavLink to="/overview">Overview</NavLink>
        <NavLink to="/signals">Signals</NavLink>
        <NavLink to="/actions">Actions</NavLink>
      </nav>
      <main className="content">
        <Routes>
          <Route path="/" element={<Upload />} />
          <Route path="/overview" element={<Overview />} />
          <Route path="/signals" element={<Signals />} />
          <Route path="/incidents/:id" element={<IncidentDetail />} />
          <Route path="/actions" element={<Actions />} />
        </Routes>
      </main>
    </div>
  )
}
