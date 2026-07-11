/**
 * Thin REST + WebSocket client for the Company AI backend.
 *
 * Stores the JWT in localStorage and attaches it to every request.
 * Surfaces server errors with a toast.
 */
(function (global) {
  const TOKEN_KEY = "oa.token";
  const USER_KEY = "oa.user";
  const BASE = "/api/v1";

  function token() { return localStorage.getItem(TOKEN_KEY) || ""; }
  function setToken(t) { localStorage.setItem(TOKEN_KEY, t); }
  function clearToken() { localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(USER_KEY); }

  // --- JWT expiry helpers ------------------------------------------------
  function jwtPayload(tok) {
    try {
      let p = (tok || "").split(".")[1] || "";
      p = p.replace(/-/g, "+").replace(/_/g, "/");
      while (p.length % 4) p += "=";
      return JSON.parse(atob(p));
    } catch { return null; }
  }
  function isExpired(tok) {
    const p = jwtPayload(tok);
    // No exp claim → let the server be the authority (treat as not expired).
    return !!(p && p.exp && (p.exp * 1000) <= Date.now());
  }

  function onLoginPage() {
    const p = location.pathname;
    return p === "/" || p === "" || p.endsWith("/index.html");
  }

  // Token expired / rejected → drop it and bounce to login (unless we're
  // already on the login page, where 401s are normal — e.g. a wrong OTP).
  function bounceToLogin() {
    if (onLoginPage()) return false;
    clearToken();
    location.replace("/index.html");
    return true;
  }

  function setUser(u) { localStorage.setItem(USER_KEY, JSON.stringify(u || {})); }
  function user() {
    try { return JSON.parse(localStorage.getItem(USER_KEY) || "{}"); }
    catch { return {}; }
  }

  function toast(msg, kind = "info") {
    const el = document.createElement("div");
    el.className = "toast" + (kind === "error" ? " error" : "");
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 4200);
  }

  async function req(method, path, body, opts = {}) {
    const headers = { "Accept": "application/json" };
    if (token()) headers["Authorization"] = "Bearer " + token();
    let payload;
    if (body instanceof FormData) {
      payload = body;
    } else if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    const res = await fetch(BASE + path, { method, headers, body: payload });
    // Session expired or token rejected → redirect to login on protected
    // pages (skips the login page so OTP errors still surface there).
    if (res.status === 401 && bounceToLogin()) {
      const err = new Error("Your session has expired. Please sign in again.");
      err.status = 401;
      throw err;
    }
    if (res.status === 204) return null;
    const ct = res.headers.get("content-type") || "";
    const data = ct.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
      // FastAPI's 422 returns `detail` as an array of validation
      // errors — turn that into something the user can act on.
      let detail = (data && data.detail);
      let message;
      if (typeof detail === "string") {
        message = detail;
      } else if (Array.isArray(detail) && detail.length) {
        message = detail.map(e => {
          const where = Array.isArray(e.loc) ? e.loc.slice(-1)[0] : "";
          return where ? `${where}: ${e.msg}` : e.msg;
        }).filter(Boolean).join("; ") || "Validation failed";
      } else {
        message = "HTTP " + res.status;
      }
      if (!opts.silent) toast(message, "error");
      const err = new Error(message);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function wsURL(path) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}${BASE}${path}?token=${encodeURIComponent(token())}`;
  }

  global.OA = {
    token, setToken, clearToken, user, setUser, toast,
    get: (p, o) => req("GET", p, undefined, o),
    post: (p, b, o) => req("POST", p, b, o),
    patch: (p, b, o) => req("PATCH", p, b, o),
    del: (p, o) => req("DELETE", p, undefined, o),
    wsURL,
    requireAuth() {
      const tok = token();
      // No token, or a locally-detectable expiry → straight to login.
      if (!tok || isExpired(tok)) {
        clearToken();
        location.replace("/index.html");
        return false;
      }
      return true;
    },
    isExpired,
  };
})(window);
