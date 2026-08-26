import { useEffect, useState } from "react";

import { callApi, formatDate, useApi } from "../../lib/api";
import { AsyncState, Card, PageHead, RunStatus } from "../common";

const SAMPLE = `[
  {
    "email": "jane@example.com",
    "first_name": "Jane",
    "last_name": "Doe",
    "company": "Example Ltd",
    "title": "Head of Operations",
    "linkedin_url": "https://www.linkedin.com/in/example"
  }
]`;

export default function Processing({ apiKey, prefillLead, onPrefillConsumed }) {
  const [leadsJson, setLeadsJson] = useState(SAMPLE);
  const [workspace, setWorkspace] = useState("");
  const [runMode, setRunMode] = useState("async");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [runId, setRunId] = useState("");
  const [runResult, setRunResult] = useState(null);

  const runs = useApi("/api/v1/runs?limit=25", apiKey, { skip: !apiKey });

  // A lead sent over from the detail drawer becomes the payload. In an effect,
  // not during render — setting state while rendering re-enters forever.
  useEffect(() => {
    if (!prefillLead) return;
    setLeadsJson(JSON.stringify([prefillLead], null, 2));
    onPrefillConsumed();
  }, [prefillLead, onPrefillConsumed]);

  async function submit(event) {
    event.preventDefault();
    let leads;
    try {
      leads = JSON.parse(leadsJson);
    } catch (e) {
      setResult({ error: `Leads JSON is not valid: ${e.message}` });
      return;
    }
    if (!Array.isArray(leads) || leads.length === 0) {
      setResult({ error: "Leads must be a non-empty JSON array." });
      return;
    }
    setBusy(true);
    setResult(null);
    const body = { leads, run_mode: runMode, source: "console" };
    if (workspace.trim()) body.workspace_slug = workspace.trim();
    const response = await callApi("/api/v1/leads/process", { method: "POST", body, apiKey });
    setBusy(false);
    setResult(response.ok ? response.data : { error: response.error });
    if (response.ok && response.data?.run_id) {
      setRunId(response.data.run_id);
      runs.reload();
    }
  }

  async function drain() {
    setRunResult({ loading: true });
    const response = await callApi("/api/v1/drain?batch=3", { method: "POST", apiKey });
    setRunResult(response.ok ? response.data : { error: response.error });
    runs.reload();
  }

  async function checkRun(id) {
    const target = (id || runId).trim();
    if (!target) return;
    setRunId(target);
    setRunResult({ loading: true });
    const response = await callApi(`/api/v1/runs/${encodeURIComponent(target)}`, { apiKey });
    setRunResult(response.ok ? response.data : { error: response.error });
  }

  return (
    <>
      <PageHead title="Run Pipeline" note="Submit leads for processing, drain the queue, and inspect run history." />
      <Card
        title="Process leads"
        hint="Runs enrichment, scoring and content generation. Never pushes to Instantly and never sends email."
      >
        <form onSubmit={submit}>
          <div className="filters">
            <div>
              <label htmlFor="ws">Workspace slug</label>
              <input
                id="ws"
                value={workspace}
                placeholder="default workspace"
                onChange={(e) => setWorkspace(e.target.value)}
              />
            </div>
            <div>
              <label htmlFor="mode">Run mode</label>
              <select id="mode" value={runMode} onChange={(e) => setRunMode(e.target.value)}>
                <option value="async">async — queue, then drain</option>
                <option value="sync">sync — wait for results</option>
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
          <p className="hint">
            <code>sync</code> must finish inside the 60s function timeout — use
            <code> async</code> for anything more than a couple of leads.
          </p>
          <div className="row">
            <button type="submit" disabled={busy}>
              {busy ? "Processing…" : "Process leads"}
            </button>
          </div>
        </form>
        {result && (
          <pre className="out">{JSON.stringify(result, null, 2)}</pre>
        )}
      </Card>

      <Card
        title="Run status"
        hint="Async runs stay queued until drained. Drain processes up to 3 at a time."
        actions={<button type="button" className="ghost" onClick={drain}>Drain queued</button>}
      >
        <div className="filters">
          <div className="grow">
            <label htmlFor="runid">Run ID</label>
            <input
              id="runid"
              value={runId}
              placeholder="run_…"
              onChange={(e) => setRunId(e.target.value)}
            />
          </div>
          <div>
            <label>&nbsp;</label>
            <button type="button" onClick={() => checkRun()}>Check</button>
          </div>
        </div>
        {runResult && (
          <pre className="out">
            {runResult.loading ? "Loading…" : JSON.stringify(runResult, null, 2)}
          </pre>
        )}

        <h3 className="section-h">Recent runs</h3>
        <AsyncState
          loading={runs.loading}
          error={runs.error}
          empty={!runs.loading && (runs.data?.runs || []).length === 0}
          emptyText="No runs yet."
        >
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>Run</th><th>Status</th><th>Mode</th><th>Source</th>
                  <th>Progress</th><th>Created</th>
                </tr>
              </thead>
              <tbody>
                {(runs.data?.runs || []).map((run) => (
                  <tr key={run.run_id} className="clickable" onClick={() => checkRun(run.run_id)}>
                    <td><code>{run.run_id}</code></td>
                    <td><RunStatus status={run.status} /></td>
                    <td className="muted">{run.run_mode}</td>
                    <td className="muted">{run.source || "—"}</td>
                    <td>
                      {run.processed_count ?? 0}/{run.lead_count ?? 0}
                      {run.failed_count > 0 && <span className="err"> ({run.failed_count} failed)</span>}
                    </td>
                    <td className="muted">{formatDate(run.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </AsyncState>
      </Card>
    </>
  );
}
