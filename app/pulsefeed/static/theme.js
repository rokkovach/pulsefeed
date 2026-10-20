/* Non-sensitive display preference only. Run before CSS to select the first paint. */
(() => {
  "use strict";
  const key = "pulsefeed-theme";
  const valid = (value) => value === "light" || value === "dark";
  let preference = null;
  let system = null;
  try { const value = localStorage.getItem(key); if (valid(value)) preference = value; } catch { /* Storage may be blocked. */ }
  try { system = window.matchMedia("(prefers-color-scheme: dark)"); } catch { /* Light is a usable fallback. */ }
  const systemTheme = () => system?.matches ? "dark" : "light";
  function updateControls() {
    const dark = document.documentElement.dataset.theme === "dark";
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.setAttribute("aria-pressed", String(dark));
      button.title = dark ? "Switch to light mode" : "Switch to dark mode";
      const icon = button.querySelector("[data-theme-icon]");
      if (icon) icon.textContent = dark ? "☾" : "☀";
    });
  }
  function apply(theme) {
    document.documentElement.dataset.theme = theme;
    updateControls();
  }
  apply(preference || systemTheme());
  function initialize() {
    updateControls();
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        preference = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
        apply(preference);
        try { localStorage.setItem(key, preference); } catch { /* Still works for this page when persistence is unavailable. */ }
      });
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, { once: true });
  else initialize();
  const systemChanged = () => { if (!preference) apply(systemTheme()); };
  if (system?.addEventListener) system.addEventListener("change", systemChanged);
  else if (system?.addListener) system.addListener(systemChanged);
  window.addEventListener("storage", (event) => {
    if (event.key === key || event.key === null) {
      preference = valid(event.newValue) ? event.newValue : null;
      apply(preference || systemTheme());
    }
  });
})();
