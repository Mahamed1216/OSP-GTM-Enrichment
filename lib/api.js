/**
 * Client-side access to the Python API on this same origin.
 *
 * The INTERNAL_API_KEY is never built into this bundle and there is no
 * unauthenticated server-side proxy. The operator pastes the key in; it lives
 * in sessionStorage (this tab only) and is sent as a bearer token straight to
 * the same-origin API.
 *
 * Nothing here throws. Every failure — a dead API, an HTML error page, an
 * offline browser — comes back as a result object so a panel can render an
 * error state instead of taking down the page.
 */
import { useCallback, useEffect, useState, useSyncExternalStore } from "react";

export const KEY_STORE = "osp.internalApiKey";

export function readStoredKey() {
  if (typeof window === "undefined") return "";
  try {
    return window.sessionStorage.getItem(KEY_STORE) || "";
  } catch {
    return "";
  }
}

const keyListeners = new Set();

export function writeStoredKey(value) {
  if (typeof window === "undefined") return;
  try {
    if (value) window.sessionStorage.setItem(KEY_STORE, value);
    else window.sessionStorage.removeItem(KEY_STORE);
  } catch {
    /* private mode: the key simply won't persist across a reload */
  }
  keyListeners.forEach((fn) => fn());
}

/**
 * The stored key as an external store.
 *
 * Read synchronously on the first client render rather than in a mount effect.
 * The effect version was fragile: whether the key reached a page depended on
 * effect ordering, and a page could render its "API key required" state with a
 * key sitting in sessionStorage. useSyncExternalStore gives React a server
 * snapshot of "" (matching the prerendered HTML) and the real value on the
 * client, so hydration stays correct without an effect.
 */
export function useStoredKey() {
  const key = useSyncExternalStore(
    (onChange) => {
      keyListeners.add(onChange);
      return () => keyListeners.delete(onChange);
    },
    () => readStoredKey(),
    () => "",
  );

  // React serves getServerSnapshot ("") while hydrating. Nudge every subscriber
  // once on mount so the real value is picked up as soon as hydration finishes,
  // rather than depending on when that happens per route.
  useEffect(() => {
    keyListeners.forEach((fn) => fn());
  }, []);

  return key;
}

export async function callApi(path, { method = "GET", body, apiKey } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`;

  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    return { ok: false, status: 0, error: `Network error: ${error}` };
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
    // A Vercel error page or any non-JSON response lands here.
    return {
      ok: false,
      status: response.status,
      error: `Expected JSON, got ${response.status} ${
        response.headers.get("content-type") || "unknown type"
      }`,
    };
  }

  if (!response.ok) {
    const detail =
      (data && (data.detail || data.error)) || `HTTP ${response.status}`;
    return { ok: false, status: response.status, error: String(detail), data };
  }
  return { ok: true, status: response.status, data };
}

/**
 * Fetch `path` whenever it (or the key) changes.
 *
 * Cancellation is a flag captured by the effect and flipped in its cleanup, so
 * a superseded request can never write state. The earlier version used a ref
 * counter with no cleanup, which under React StrictMode's double-invoked
 * effects left every response discarded and the panel stuck on "Loading…".
 */
export function useApi(path, apiKey, { skip = false } = {}) {
  const [state, setState] = useState({ loading: !skip, data: null, error: null });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (skip || !path) {
      setState({ loading: false, data: null, error: null });
      return undefined;
    }
    let cancelled = false;
    setState((prev) => ({ ...prev, loading: true }));
    callApi(path, { apiKey }).then((result) => {
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
  }, [path, apiKey, skip, nonce]);

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
