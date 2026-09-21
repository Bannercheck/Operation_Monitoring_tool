// Theme: dark (the ops-room default), light, or follow the operating system. The choice lives in this browser only.
import { createContext, useContext } from "react";

export type Theme = "dark" | "system" | "light";
export const ThemeCtx = createContext<{ theme: Theme; setTheme: (t: Theme) => void }>({ theme: "dark", setTheme: () => {} });
export const useTheme = () => useContext(ThemeCtx);

export function applyTheme(t: Theme) {
  const light = t === "light" || (t === "system" && window.matchMedia("(prefers-color-scheme: light)").matches);
  document.documentElement.classList.toggle("light", light);
  document.documentElement.setAttribute("data-theme", t);
  document.querySelector('meta[name="color-scheme"]')?.setAttribute("content", light ? "light" : "dark");
}
