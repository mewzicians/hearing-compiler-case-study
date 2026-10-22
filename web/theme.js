/* Apply the preference before styles load; source data is never stored here. */
(() => {
  const key = "hearing-compiler-theme";
  const cookieKey = "hearing_compiler_theme";
  const fallbackKey = key + "-local-fallback";
  const root = document.documentElement;
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let choice = null;
  let notice = "";
  function readCookie() {
    try {
      const entry = document.cookie.split(";").map(value => value.trim())
        .find(value => value.startsWith(cookieKey + "="));
      const value = entry?.slice(cookieKey.length + 1);
      return value === "dark" || value === "light" ? value : null;
    } catch (_) { return null; }
  }
  try {
    const saved = window.localStorage.getItem(key);
    const shared = readCookie();
    let fallback = null;
    try { fallback = JSON.parse(window.localStorage.getItem(fallbackKey)); } catch (_) {}
    const localOnly = fallback && fallback.choice === saved && fallback.shared === shared
      && (saved === "dark" || saved === "light");
    choice = localOnly ? saved : shared || (saved === "dark" || saved === "light" ? saved : null);
    if (!localOnly && shared) {
      window.localStorage.setItem(key, shared);
      window.localStorage.removeItem(fallbackKey);
    }
    if (localOnly) notice = "Appearance is saved for this address; it may reset when the app reopens on another port.";
  } catch (_) {
    choice = readCookie();
    notice = "Browser storage is unavailable; appearance may need to be chosen again.";
  }

  function remember() {
    let local = false;
    try { window.localStorage.setItem(key, choice); local = true; } catch (_) {}
    try { document.cookie = `${cookieKey}=${choice}; Path=/; SameSite=Strict; Max-Age=31536000`; } catch (_) {}
    const shared = readCookie();
    if (shared === choice) {
      try { window.localStorage.removeItem(fallbackKey); } catch (_) {}
    } else if (local && shared !== null) {
      try { window.localStorage.setItem(fallbackKey, JSON.stringify({choice, shared})); }
      catch (_) { local = false; }
    }
    notice = shared === choice ? "" : local
      ? "Appearance is saved for this address; it may reset when the app reopens on another port."
      : "Your browser could not save appearance; choose it again after reopening.";
  }
  // Migrate an explicit preference saved by the earlier origin-only version.
  // A system default is never promoted into a saved explicit choice.
  if (choice !== null && readCookie() === null) remember();

  function apply(theme) {
    root.dataset.theme = theme;
    const button = document.getElementById("theme-toggle");
    if (button) {
      button.setAttribute("aria-pressed", String(theme === "dark"));
      button.textContent = theme === "dark" ? "Light mode" : "Dark mode";
    }
    const status = document.getElementById("theme-status");
    if (status) { status.textContent = notice; status.hidden = !notice; }
  }
  apply(choice || (system.matches ? "dark" : "light"));
  system.addEventListener("change", event => {
    if (choice === null) apply(event.matches ? "dark" : "light");
  });
  window.addEventListener("storage", event => {
    if (event.key !== key) return;
    choice = event.newValue === "dark" || event.newValue === "light" ? event.newValue : null;
    apply(choice || (system.matches ? "dark" : "light"));
  });
  document.addEventListener("DOMContentLoaded", () => {
    apply(root.dataset.theme);
    document.getElementById("theme-toggle").addEventListener("click", () => {
      choice = root.dataset.theme === "dark" ? "light" : "dark";
      remember();
      apply(choice);
    });
  });
})();
