import { useEffect } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "./auth";
import Layout from "./components/Layout";
import Login from "./pages/Login";
import Ops from "./pages/Ops";
import Anomalies from "./pages/Anomalies";
import Datasets from "./pages/Datasets";
import DatasetDetail from "./pages/DatasetDetail";
import Actions from "./pages/Actions";
import Playbook from "./pages/Playbook";
import Connections from "./pages/Connections";
import Knowledge from "./pages/Knowledge";
import Notify from "./pages/Notify";
import Users from "./pages/Users";
import System from "./pages/System";
import MapPage from "./pages/Map";
import Assist from "./pages/Assist";
import Llm from "./pages/Llm";
import Itsm from "./pages/Itsm";
import Readme from "./pages/Readme";

export default function App() {
  const { user, ready, acceptToken } = useAuth();
  const loc = useLocation();
  const nav = useNavigate();
  useEffect(() => {                                   // SSO callback lands on /#sso=<token>
    const m = /sso=([^&]+)/.exec(loc.hash);
    if (m) { acceptToken(m[1]).then(() => nav(loc.pathname || "/", { replace: true })); }
  }, [loc.hash, loc.pathname, acceptToken, nav]);
  if (!ready) return <div className="center muted">…</div>;
  if (!user) return <Routes><Route path="*" element={<Login />} /></Routes>;
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/ops" replace />} />
        <Route path="/ops" element={<Ops />} />
        <Route path="/anomalies" element={<Anomalies />} />
        <Route path="/datasets" element={<Datasets />} />
        <Route path="/datasets/:key" element={<DatasetDetail />} />
        <Route path="/actions" element={<Actions />} />
        <Route path="/playbook" element={<Playbook />} />
        <Route path="/connections" element={<Connections />} />
        <Route path="/knowledge" element={<Knowledge />} />
        <Route path="/alerts" element={<Notify />} />
        <Route path="/users" element={<Users />} />
        <Route path="/system" element={<System />} />
        <Route path="/map" element={<MapPage />} />
        <Route path="/assist" element={<Assist />} />
        <Route path="/llm" element={<Llm />} />
        <Route path="/itsm" element={<Itsm />} />
        <Route path="/readme" element={<Readme />} />
        <Route path="/login" element={<Navigate to="/ops" replace />} />
        <Route path="*" element={<Navigate to="/ops" replace />} />
      </Route>
    </Routes>
  );
}
