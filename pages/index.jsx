import Head from "next/head";
import { useCallback, useEffect, useState } from "react";

/**
 * Operator console.
 *
 * Auth note: /api/v1/* requires the INTERNAL_API_KEY as a bearer token. The key
 * is NOT baked into this bundle and there is no unauthenticated server-side
 * proxy — the operator pastes it here, it is kept in sessionStorage (this
 * browser tab only, cleared when the tab closes) and sent straight to the
 * same-origin API.
 */

const KEY_STORE = "osp.internalApiKey";
const RUNS_STORE = "osp.recentRuns";

const SAMPLE_LEADS = `[
  {
    "email": "jane@example.com",
    "first_name": "Jane",
    "last_name": "Doe",
    "company": "Example Ltd",
    "title": "Head of Operations",
    "linkedin_url": "https://www.linkedin.com/in/example"
  }
]`;

function readSession(key, fallback) {
  if (typeof window === "undefined") return fallback;
  try {
    const raw = window.sessionStorage.getItem(key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

function writeSession(key, value) {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Private mode or storage disabled: the console still works, the key just
    // does not persist across a reload.
  }
}

async function callApi(path, { method = "GET", body, apiKey } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    data = { raw: text.slice(0, 2000) };
  }
  return { ok: res.ok, status: res.status, data };
}

export default function Home() {
  const [apiKey, setApiKey] = useState("");
  const [keySaved, setKeySaved] = useState(false);

  const [health, setHealth] = useState({ state: "loading" });
  const [info, setInfo] = useState(null);

  const [workspace, setWorkspace] = useState("");
  const [runMode, setRunMode] = useState("async");
  const [leadsJson, setLeadsJson] = useState(SAMPLE_LEADS);
  const [processing, setProcessing] = useState(false);
  const [processOut, setProcessOut] = useState(null);

  const [runId, setRunId] = useState("");
  const [runOut, setRunOut] = useState(null);
  const [recentRuns, setRecentRuns] = useState([]);

  useEffect(() => {
    const stored = readSession(KEY_STORE, "");
    if (stored) {
      setApiKey(stored);
      setKeySaved(true);
    }
    setRecentRuns(readSession(RUNS_STORE, []));
  }, []);

  const refreshHealth = useCallback(async () => {
    setHealth({ state: "loading" });
    try {
      const [h, i] = await Promise.all([callApi("/health"), callApi("/api/info")]);
      setHealth(h.ok ? { state: "ok", data: h.data } : { state: "err", status: h.status });
      setInfo(i.ok ? i.data : null);
    } catch (e) {
      setHealth({ state: "err", message: String(e) });
    }
  }, []);

  useEffect(() => {
    refreshHealth();
  }, [refreshHealth]);

  function rememberRun(id) {
    if (!id) return;
    setRecentRuns((prev) => {
      const next = [id, ...prev.filter((r) => r !== id)].slice(0, 8);
      writeSession(RUNS_STORE, next);
      return next;
    });
  }

  async function submitLeads(event) {
    event.preventDefault();
    if (!apiKey) {
      setProcessOut({ error: "Enter the internal API key first." });
      return;
    }
    let leads;
    try {
      leads = JSON.parse(leadsJson);
    } catch (e) {
      setProcessOut({ error: `Leads JSON is not valid: ${e.message}` });
      return;
    }
    if (!Array.isArray(leads) || leads.length === 0) {
      setProcessOut({ error: "Leads must be a non-empty JSON array." });
      return;
    }

    setProcessing(true);
    setProcessOut(null);
    const body = { leads, run_mode: runMode, source: "operator-console" };
    if (workspace.trim()) body.workspace_slug = workspace.trim();
    const res = await callApi("/api/v1/leads/process", { method: "POST", body, apiKey });
    setProcessing(false);
    setProcessOut(res);
    if (res.ok && res.data && res.data.run_id) {
      rememberRun(res.data.run_id);
      setRunId(res.data.run_id);
    }
  }

  async function checkRun(id) {
    const target = (id || runId).trim();
    if (!target) return;
    if (!apiKey) {
      setRunOut({ error: "Enter the internal API key first." });
      return;
    }
    setRunId(target);
    setRunOut({ loading: true });
    const res = await callApi(`/api/v1/runs/${encodeURIComponent(target)}`, { apiKey });
    setRunOut(res);
    if (res.ok) rememberRun(target);
  }

  async function drain() {
    if (!apiKey) {
      setRunOut({ error: "Enter the internal API key first." });
      return;
    }
    setRunOut({ loading: true });
    setRunOut(await callApi("/api/v1/drain?batch=3", { method: "POST", apiKey }));
  }

  const healthDot =
    health.state === "ok" ? "ok" : health.state === "loading" ? "warn" : "err";

  return (
    <>
      <Head>
        <title>OSP GTM Enrichment — Operator Console</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="robots" content="noindex" />
      </Head>

      <main className="wrap">
        <header className="top">
          <h1>OSP GTM Enrichment</h1>
          <span className="pill">
            <span className={`dot ${healthDot}`} />
            {health.state === "ok"
              ? `API ${health.data ? health.data.status : "ok"}`
              : health.state === "loading"
                ? "checking API…"
                : "API unreachable"}
          </span>
        </header>
        <p className="sub">
          Operator console. The enrichment pipeline runs behind the API on this
          same origin — endpoints stay available under <code>/api</code>.
        </p>

        <div className="grid">
          <section className="card span">
            <h2>Access</h2>
            <p className="hint">
              <code>/api/v1/*</code> needs the internal API key. It is never
              stored in this deployment — it stays in this browser tab
              (sessionStorage) and is sent directly to the same-origin API.
            </p>
            <div className="row">
              <div>
                <label htmlFor="key">INTERNAL_API_KEY</label>
                <input
                  id="key"
                  type="password"
                  value={apiKey}
                  placeholder="paste the internal API key"
                  autoComplete="off"
                  onChange={(e) => {
                    setApiKey(e.target.value);
                    setKeySaved(false);
                  }}
                />
              </div>
              <div className="shrink">
                <button
                  type="button"
                  onClick={() => {
                    writeSession(KEY_STORE, apiKey);
                    setKeySaved(true);
                  }}
                  disabled={!apiKey}
                >
                  {keySaved ? "Saved for this tab" : "Keep for this tab"}
                </button>
              </div>
              <div className="shrink">
                <button
                  type="button"
                  className="ghost"
                  onClick={() => {
                    setApiKey("");
                    setKeySaved(false);
                    writeSession(KEY_STORE, "");
                  }}
                >
                  Forget key
                </button>
              </div>
            </div>
          </section>

          <section className="card">
            <h2>Health</h2>
            <p className="hint">
              <code>GET /health</code> — public, no key required.
            </p>
            <dl className="kv">
              <dt>Status</dt>
              <dd>{health.state === "ok" ? health.data.status : health.state}</dd>
              <dt>Service</dt>
              <dd>{health.data ? health.data.service : "—"}</dd>
              <dt>API version</dt>
              <dd>{health.data ? health.data.version : "—"}</dd>
              <dt>Database</dt>
              <dd>
                {info === null
                  ? "—"
                  : info.database_configured
                    ? "DATABASE_URL configured"
                    : "not configured"}
              </dd>
            </dl>
            {info !== null && !info.database_configured && (
              <p className="hint err" style={{ marginTop: ".7rem" }}>
                DATABASE_URL is unset — every database-backed endpoint will fail.
              </p>
            )}
            <div className="row" style={{ marginTop: ".9rem" }}>
              <div className="shrink">
                <button type="button" className="ghost" onClick={refreshHealth}>
                  Refresh
                </button>
              </div>
            </div>
          </section>

          <section className="card">
            <h2>Runs &amp; status</h2>
            <p className="hint">
              <code>GET /api/v1/runs/{"{run_id}"}</code>. Async runs are drained
              by <code>POST /api/v1/drain</code>.
            </p>
            <label htmlFor="runid">Run ID</label>
            <div className="row">
              <div>
                <input
                  id="runid"
                  value={runId}
                  placeholder="run_…"
                  onChange={(e) => setRunId(e.target.value)}
                />
              </div>
              <div className="shrink">
                <button type="button" onClick={() => checkRun()}>
                  Check
                </button>
              </div>
              <div className="shrink">
                <button type="button" className="ghost" onClick={drain}>
                  Drain queued
                </button>
              </div>
            </div>
            {recentRuns.length > 0 && (
              <>
                <label>Recent (this tab)</label>
                <ul className="plain">
                  {recentRuns.map((r) => (
                    <li key={r}>
                      <button
                        type="button"
                        className="ghost"
                        style={{ padding: ".1rem .3rem", border: 0 }}
                        onClick={() => checkRun(r)}
                      >
                        <code>{r}</code>
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {runOut && (
              <pre className="out">
                {runOut.loading
                  ? "Loading…"
                  : JSON.stringify(runOut.error || runOut, null, 2)}
              </pre>
            )}
          </section>

          <section className="card span">
            <h2>Process leads</h2>
            <p className="hint">
              <code>POST /api/v1/leads/process</code>. Never pushes to Instantly
              and never sends email. <code>sync</code> must finish inside the
              function timeout — use <code>async</code> for anything larger than
              a couple of leads.
            </p>
            <form onSubmit={submitLeads}>
              <div className="row">
                <div>
                  <label htmlFor="ws">Workspace slug</label>
                  <input
                    id="ws"
                    value={workspace}
                    placeholder="e.g. osp"
                    onChange={(e) => setWorkspace(e.target.value)}
                  />
                </div>
                <div>
                  <label htmlFor="mode">Run mode</label>
                  <select
                    id="mode"
                    value={runMode}
                    onChange={(e) => setRunMode(e.target.value)}
                  >
                    <option value="async">async (queue, then drain)</option>
                    <option value="sync">sync (wait for results)</option>
                  </select>
                </div>
              </div>
              <label htmlFor="leads">Leads (JSON array)</label>
              <textarea
                id="leads"
                value={leadsJson}
                spellCheck={false}
                onChange={(e) => setLeadsJson(e.target.value)}
              />
              <div className="row" style={{ marginTop: ".8rem" }}>
                <div className="shrink">
                  <button type="submit" disabled={processing}>
                    {processing ? "Processing…" : "Process leads"}
                  </button>
                </div>
              </div>
            </form>
            {processOut && (
              <pre className="out">
                {JSON.stringify(processOut.error || processOut, null, 2)}
              </pre>
            )}
          </section>

          <section className="card span">
            <h2>Webhooks &amp; scheduled jobs</h2>
            <p className="hint">
              Machine-to-machine endpoints. They authenticate with their own
              secret headers, set as environment variables on this deployment —
              there is nothing to trigger from the browser.
            </p>
            <ul className="plain">
              <li>
                <span className="method">POST</span>
                <code>/api/instantly/reply-webhook</code> — header{" "}
                <code>X-Webhook-Secret</code>. Point the Instantly &ldquo;Lead
                Replied&rdquo; automation here.
              </li>
              <li>
                <span className="method">POST</span>
                <code>/api/lead-source/run-scheduled</code> — header{" "}
                <code>X-Job-Secret</code>. Evergreen lead-source import.
              </li>
              <li>
                <span className="method">POST</span>
                <code>/api/v1/drain</code> — bearer key or{" "}
                <code>CRON_SECRET</code>. Drains queued async runs; wire it to a
                Vercel Cron.
              </li>
              <li>
                <span className="method">GET</span>
                <code>/api/v1/leads/{"{id}"}/processed</code> — bearer key.
                Processed payload for one lead.
              </li>
            </ul>
          </section>
        </div>

        <p className="note">
          This console covers the API surface only. The full operator UI (leads,
          lead detail, prompts, settings) is the Streamlit app in{" "}
          <code>app/</code>, which cannot run on Vercel — it needs a long-lived
          stateful process, so it stays on Streamlit Cloud against the same{" "}
          <code>DATABASE_URL</code>.
        </p>
      </main>
    </>
  );
}
