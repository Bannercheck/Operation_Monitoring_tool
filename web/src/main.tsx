import React, { useState } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { AuthProvider } from "./auth";
import { LangCtx, type Lang } from "./i18n";
import "./styles.css";

const qc = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 5000 } } });

function Root() {
  const [lang, setLangState] = useState<Lang>(() => { try { return (localStorage.getItem("wo_lang") as Lang) || "tr"; } catch { return "tr"; } });
  const setLang = (l: Lang) => { setLangState(l); try { localStorage.setItem("wo_lang", l); } catch { /* ignore */ } document.documentElement.lang = l; };
  return (
    <LangCtx.Provider value={{ lang, setLang }}>
      <QueryClientProvider client={qc}>
        <AuthProvider>
          <BrowserRouter><App /></BrowserRouter>
        </AuthProvider>
      </QueryClientProvider>
    </LangCtx.Provider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(<React.StrictMode><Root /></React.StrictMode>);
