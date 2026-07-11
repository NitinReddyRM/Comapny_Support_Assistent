/**
 * Theme toggle.
 *
 * Drives two flavours of UI:
 *   1. Old single-button toggle (#themeToggle / #themeBtn) — still used
 *      on the login screen + admin sidebar foot.
 *   2. New sliding switch (#themeSwitchInput) — used in chat sidebar.
 *
 * Persists the choice in localStorage. Icons swap to the active state's
 * "opposite" so the user sees what they'll *get* on click (Claude
 * convention: in dark mode you see the sun icon to switch to light).
 */
(function () {
  const KEY = "oa.theme";
  const root = document.documentElement;
  const initial = localStorage.getItem(KEY) || root.getAttribute("data-theme") || "dark";
  root.setAttribute("data-theme", initial);

  function isDark() { return root.getAttribute("data-theme") === "dark"; }

  function paint() {
    if (!window.Icons) return;
    // Single-button toggles: show the OPPOSITE icon (action affordance).
    const btnIcon = isDark() ? window.Icons.sun : window.Icons.moon;
    document.querySelectorAll("#themeToggle, #themeBtn").forEach(b => { b.innerHTML = btnIcon; });

    // Switch checkbox state + label
    const sw = document.getElementById("themeSwitchInput");
    if (sw) sw.checked = isDark();
    const lbl = document.getElementById("themeLabel");
    if (lbl) lbl.textContent = isDark() ? "Dark" : "Light";
  }

  function setTheme(t) {
    root.setAttribute("data-theme", t);
    localStorage.setItem(KEY, t);
    paint();
  }
  function toggle() { setTheme(isDark() ? "light" : "dark"); }

  // Wire interactions: legacy buttons + new switch.
  document.addEventListener("click", (e) => {
    if (e.target.closest && e.target.closest("#themeToggle, #themeBtn")) toggle();
  });
  document.addEventListener("change", (e) => {
    if (e.target && e.target.id === "themeSwitchInput") {
      setTheme(e.target.checked ? "dark" : "light");
    }
  });

  document.addEventListener("DOMContentLoaded", paint);
  paint();
})();
