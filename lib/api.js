/**
 * Client-side access to the Python API on this same origin.
 *
 * Auth is an admin session cookie, set server-side by POST /api/auth/login and
 * flagged HttpOnly, so no credential is readable from JavaScript.
 */
import { useCallback, useEffect, useState } from "react";

/**
 * Call the same-origin API.
 *
 * Auth is an HttpOnly session cookie set by POST /api/auth/login. The browser
 * never holds a credential: no key in this bundle, nothing in localStorage or
 * sessionStorage, and INTERNAL_API_KEY stays server-side for
 * backend-to-backend callers.
 *
 * Never throws. A dead API, an HTML error page or an offline browser all come
 * back as a result object so a panel can render an error state.
 */
export async function callApi(path, { method = "GET", body } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: "same-origin", // send the session cookie
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    return { ok: false, status: 0, unreachable: true, error: `Network error: ${error}` };
  }

  let text = "";
  try {
    text = await response.text();
  } catch {
    /* connection dropped mid-read */
  }

  let data;
  try {
    data = JSON.parse(text);
  } catch {
    // A plain-text 500 or an HTML error page. Name the route so the panel
    // shows something actionable rather than a bare parse failure.
    const type = (response.headers.get("content-type") || "unknown type").split(";")[0];
    return {
      ok: false,
      status: response.status,
      error: `API returned ${response.status} ${type} from ${path}`,
      raw: text.slice(0, 300),
    };
  }

  if (!response.ok) {
    // The API answers errors as {error, message, hint, request_id}. Prefer the
    // human-readable parts, and keep the hint — it usually says what to fix.
    const message =
      (data && (data.message || data.detail || data.error)) ||
      `HTTP ${response.status}`;
    const hint = data && data.hint ? ` ${data.hint}` : "";
    return {
      ok: false,
      status: response.status,
      error: `${message}${hint}`,
      data,
    };
  }
  return { ok: true, status: response.status, data };
}

export async function login(password) {
  return callApi("/api/auth/login", { method: "POST", body: { password } });
}

export async function logout() {
  return callApi("/api/auth/logout", { method: "POST" });
}

/**
 * Fetch `path` whenever it (or the key) changes.
 *
 * Cancellation is a flag captured by the effect and flipped in its cleanup, so
 * a superseded request can never write state. The earlier version used a ref
 * counter with no cleanup, which under React StrictMode's double-invoked
 * effects left every response discarded and the panel stuck on "Loading…".
 */
export function useApi(path, { skip = false } = {}) {
  const [state, setState] = useState({ loading: !skip, data: null, error: null });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (skip || !path) {
      setState({ loading: false, data: null, error: null });
      return undefined;
    }
    let cancelled = false;
    setState((prev) => ({ ...prev, loading: true }));
    callApi(path).then((result) => {
      if (cancelled) return;
      setState({
        loading: false,
        data: result.ok ? result.data : null,
        error: result.ok ? null : result.error,
      });
    });
    return () => {
      cancelled = true;
    };
  }, [path, skip, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { ...state, reload };
}

export function formatDate(value) {
  if (!value) return "—";
  try {
    return new Date(value).toLocaleString(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    });
  } catch {
    return String(value);
  }
}
