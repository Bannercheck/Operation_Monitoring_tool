import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { AuthProvider } from "./auth";
import { LangCtx, type Lang } from "./i18n";
import { ThemeCtx, applyTheme, type Theme } from "./theme";
import "./styles.css";

const qc = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 5000 } } });

function Root() {
  const [lang, setLangState] = useState<Lang>(() => { try { return (localStorage.getItem("wo_lang") as Lang) || "tr"; } catch { return "tr"; } });
  const setLang = (l: Lang) => { setLangState(l); try { localStorage.setItem("wo_lang", l); } catch { /* ignore */ } document.documentElement.lang = l; };
  useEffect(() => { document.documentElement.lang = lang; }, [lang]);   // also on first load: CSS text-transform must follow the UI language (installed -> INSTALLED, not İNSTALLED)
  const [theme, setThemeState] = useState<Theme>(() => { try { return (localStorage.getItem("wo_theme") as Theme) || "dark"; } catch { return "dark"; } });
  const setTheme = (t: Theme) => { setThemeState(t); try { localStorage.setItem("wo_theme", t); } catch { /* ignore */ } applyTheme(t); };
  useEffect(() => { applyTheme(theme); const mq = window.matchMedia("(prefers-color-scheme: light)"); const h = () => applyTheme(theme); mq.addEventListener("change", h); return () => mq.removeEventListener("change", h); }, [theme]);
  return (
    <ThemeCtx.Provider value={{ theme, setTheme }}>
    <LangCtx.Provider value={{ lang, setLang }}>
      <QueryClientProvider client={qc}>
        <AuthProvider>
          <BrowserRouter><App /></BrowserRouter>
        </AuthProvider>
      </QueryClientProvider>
    </LangCtx.Provider>
    </ThemeCtx.Provider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(<React.StrictMode><Root /></React.StrictMode>);
