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
import { useCallback, useEffect, useRef, useState } from "react";

export const KEY_STORE = "osp.internalApiKey";

export function readStoredKey() {
  if (typeof window === "undefined") return "";
  try {
    return window.sessionStorage.getItem(KEY_STORE) || "";
  } catch {
    return "";
  }
}

export function writeStoredKey(value) {
  if (typeof window === "undefined") return;
  try {
    if (value) window.sessionStorage.setItem(KEY_STORE, value);
    else window.sessionStorage.removeItem(KEY_STORE);
  } catch {
    /* private mode: the key simply won't persist across a reload */
  }
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
 * Fetch `path` whenever it (or the key) changes. Returns loading/error/data
 * plus a `reload` for manual refresh. Ignores responses from superseded
 * requests so fast filter changes can't render stale rows.
 */
export function useApi(path, apiKey, { skip = false } = {}) {
  const [state, setState] = useState({ loading: !skip, data: null, error: null });
  const requestId = useRef(0);

  const load = useCallback(async () => {
    if (skip || !path) return;
    const id = ++requestId.current;
    setState((prev) => ({ ...prev, loading: true }));
    const result = await callApi(path, { apiKey });
    if (id !== requestId.current) return; // a newer request has started
    setState({
      loading: false,
      data: result.ok ? result.data : null,
      error: result.ok ? null : result.error,
    });
  }, [path, apiKey, skip]);

  useEffect(() => {
    load();
  }, [load]);

  return { ...state, reload: load };
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
