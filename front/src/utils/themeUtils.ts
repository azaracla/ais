/**
 * Get initial theme from localStorage or system preference
 */
export function getInitialTheme(): "light" | "dark" {
  try {
    const stored = localStorage.getItem("ais-theme");
    if (stored === "dark" || stored === "light") return stored;
  } catch {
    /* localStorage unavailable — use default */
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}
