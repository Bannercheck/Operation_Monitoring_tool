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
import Inbox from "./pages/Inbox";
import Playbook from "./pages/Playbook";
import Connections from "./pages/Connections";
import SourcesPage from "./pages/Sources";
import InventoryPage from "./pages/Inventory";
import Scan from "./pages/Scan";
import Knowledge from "./pages/Knowledge";
import Notify from "./pages/Notify";
import Users from "./pages/Users";
import System from "./pages/System";
import MapPage from "./pages/apm/Topology";
import Assist from "./pages/Assist";
import Llm from "./pages/Llm";
import Itsm from "./pages/Itsm";
import Readme from "./pages/Readme";
import Services from "./pages/apm/Services";
import ServiceDetail from "./pages/apm/ServiceDetail";
import Traces from "./pages/apm/Traces";
import TraceDetail from "./pages/apm/TraceDetail";
import Rum from "./pages/rum/Rum";
import RumSession from "./pages/rum/Session";
import Integrations from "./pages/apm/Integrations";
import Flows from "./pages/apm/Flows";
import Exceptions from "./pages/apm/Exceptions";
import Thresholds from "./pages/Thresholds";
import MyTeam from "./pages/MyTeam";
import Logs from "./pages/Logs";
import Apps from "./pages/apps/Apps";
import AppDetail from "./pages/apps/AppDetail";

export default function App() {
  const { user, ready, acceptToken, can } = useAuth();
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
        <Route path="/logs" element={can("page.logs") ? <Logs /> : <Navigate to="/ops" replace />} />
        <Route path="/apps" element={can("page.apps") ? <Apps /> : <Navigate to="/ops" replace />} />
        <Route path="/apps/:id" element={can("page.apps") ? <AppDetail /> : <Navigate to="/ops" replace />} />
        <Route path="/anomalies" element={<Anomalies />} />
        <Route path="/datasets" element={<Datasets />} />
        <Route path="/datasets/:key" element={<DatasetDetail />} />
        <Route path="/actions" element={<Actions />} />
        <Route path="/inbox" element={<Inbox />} />
        <Route path="/playbook" element={<Playbook />} />
        <Route path="/connections" element={<Connections />} />
        <Route path="/sources" element={<SourcesPage />} />
        <Route path="/inventory" element={<InventoryPage />} />
        <Route path="/scan" element={<Scan />} />
        <Route path="/parser" element={<Navigate to="/llm?tab=parser" replace />} />
        <Route path="/knowledge" element={<Knowledge />} />
        <Route path="/alerts" element={<Notify />} />
        <Route path="/users" element={<Users />} />
        <Route path="/team" element={<MyTeam />} />
        <Route path="/system" element={<System />} />
        <Route path="/map" element={<MapPage />} />
        <Route path="/services" element={<Services />} />
        <Route path="/services/:name" element={<ServiceDetail />} />
        <Route path="/traces" element={<Traces />} />
        <Route path="/traces/:id" element={<TraceDetail />} />
        <Route path="/rum" element={<Rum />} />
        <Route path="/integrations" element={<Integrations />} />
        <Route path="/flows" element={<Flows />} />
        <Route path="/exceptions" element={<Exceptions />} />
        <Route path="/thresholds" element={<Thresholds />} />
        <Route path="/rum/sessions/:id" element={<RumSession />} />
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
