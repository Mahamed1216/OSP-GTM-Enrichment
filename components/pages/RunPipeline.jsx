import { useEffect, useMemo, useState } from "react";

import { callApi, formatDate, useApi } from "../../lib/api";
import { AsyncState, Card, PageHead, RunStatus, Section } from "../common";
import { PREFILL_STORE } from "../Shell";

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

// Actions run in this order; the checkboxes below mirror it.
const ACTIONS = [
  ["enrichment", "Enrichment"],
  ["field_service_fit", "Field service fit research"],
  ["scoring", "Scoring"],
  ["email", "Email generation"],
  ["push", "Push to Instantly"],
];

const PRESETS = {
  all: ["enrichment", "field_service_fit", "scoring", "email"],
  score_only: ["scoring"],
  enrich_score: ["enrichment", "scoring"],
  email_only: ["email"],
  missing_emails: ["email"],
};

const SELECTIONS = [
  ["unprocessed", "All unprocessed / unscored leads"],
  ["all", "All leads"],
  ["rows", "Specific lead row numbers or ranges"],
  ["tiers", "Selected tiers"],
  ["statuses", "Selected statuses"],
];

export default function RunPipeline({ authed }) {
  const [selection, setSelection] = useState("unprocessed");
  const [rows, setRows] = useState("");
  const [tiers, setTiers] = useState(["A", "B"]);
  const [statuses, setStatuses] = useState(["new"]);
  const [actions, setActions] = useState(new Set(PRESETS.all));
  const [leadsJson, setLeadsJson] = useState(SAMPLE);
  const [workspace, setWorkspace] = useState("");
  const [runMode, setRunMode] = useState("async");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [runId, setRunId] = useState("");
  const [runResult, setRunResult] = useState(null);
  const [openPanel, setOpenPanel] = useState("select");

  const counts = useApi("/api/v1/leads?limit=1", { skip: !authed });
  const runs = useApi("/api/v1/runs?limit=25", { skip: !authed });
  const total = counts.data?.total ?? 0;

  // A lead handed over from the detail drawer becomes the payload.
  useEffect(() => {
    try {
      const stored = window.sessionStorage.getItem(PREFILL_STORE);
      if (!stored) return;
      setLeadsJson(JSON.stringify([JSON.parse(stored)], null, 2));
      setSelection("payload");
      window.sessionStorage.removeItem(PREFILL_STORE);
    } catch {
      /* nothing handed over */
    }
  }, []);

  const selectedCount = useMemo(() => {
    if (selection === "all") return total;
    if (selection === "rows") {
      // "1-5, 9" -> 6
      return rows.split(",").reduce((sum, part) => {
        const [a, b] = part.split("-").map((n) => parseInt(n.trim(), 10));
        if (Number.isNaN(a)) return sum;
        return sum + (Number.isNaN(b) ? 1 : Math.max(0, b - a + 1));
      }, 0);
    }
    if (selection === "payload") {
      try {
        return JSON.parse(leadsJson).length;
      } catch {
        return 0;
      }
    }
    return null; // server-side filter — count is not known client-side
  }, [selection, rows, total, leadsJson]);

  function toggleAction(id) {
    setActions((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function applyPreset(name) {
    setActions(new Set(PRESETS[name]));
  }

  async function run(event) {
    event.preventDefault();
    if (!authed) {
      setResult({ error: "Log in with the admin password above to run the pipeline." });
      return;
    }
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
    const body = {
      leads,
      run_mode: runMode,
      source: "console",
      options: {
        enrich: actions.has("enrichment"),
        score: actions.has("scoring"),
        generate_email: actions.has("email"),
        field_service_fit: actions.has("field_service_fit"),
        // Push is never honoured by the API — it refuses push_to_instantly.
        push_to_instantly: false,
      },
    };
    if (workspace.trim()) body.workspace_slug = workspace.trim();

    const response = await callApi("/api/v1/leads/process", { method: "POST", body });
    setBusy(false);
    setResult(response.ok ? response.data : { error: response.error });
    if (response.ok && response.data?.run_id) {
      setRunId(response.data.run_id);
      runs.reload();
    }
  }

  async function drain() {
    if (!authed) {
      setRunResult({ error: "Log in with the admin password to drain the queue." });
      return;
    }
    setRunResult({ loading: true });
    const response = await callApi("/api/v1/drain?batch=3", { method: "POST" });
    setRunResult(response.ok ? response.data : { error: response.error });
    runs.reload();
  }

  async function checkRun(id) {
    const target = (id || runId).trim();
    if (!target || !authed) return;
    setRunId(target);
    setRunResult({ loading: true });
    const response = await callApi(`/api/v1/runs/${encodeURIComponent(target)}`);
    setRunResult(response.ok ? response.data : { error: response.error });
  }

  const panel = (id, title, note, children) => (
    <div className="accordion">
      <button
        type="button"
        className="accordion-head"
        aria-expanded={openPanel === id}
        onClick={() => setOpenPanel(openPanel === id ? "" : id)}
      >
        <span className="accordion-caret">{openPanel === id ? "▾" : "▸"}</span>
        <span className="accordion-title">{title}</span>
        {note && <span className="muted accordion-size">{note}</span>}
      </button>
      {openPanel === id && <div className="accordion-body">{children}</div>}
    </div>
  );

  return (
    <>
      <PageHead
        title="Run Pipeline."
        note="Enrich, score and generate for the leads you select. Nothing is sent from here."
      />

      {!authed && (
        <div className="empty" style={{ marginBottom: "1.25rem" }}>
          <strong>Sign in required</strong>
          Log in with the admin password above to run the pipeline or drain the queue.
        </div>
      )}

      <div className="accordions">
        {panel("ingest", "A. Ingest leads (upload CSV / Excel)", "not configured", (
          <>
            <div className="dropzone">
              <strong>CSV / Excel upload is not wired up yet</strong>
              <p className="hint">
                There is no upload endpoint on the API. Import today with the
                ingest scripts, the lead-source API, or paste JSON below.
              </p>
            </div>
          </>
        ))}

        {panel("select", "Select leads to process", `${selectedCount ?? "—"} selected`, (
          <>
            <p className="hint">
              {counts.loading
                ? "Counting leads…"
                : `${total} lead${total === 1 ? "" : "s"} in the workspace. Row numbers refer to the full ordered list.`}
            </p>
            <div className="radios">
              {SELECTIONS.map(([id, label]) => (
                <label key={id} className="radio">
                  <input
                    type="radio"
                    name="selection"
                    checked={selection === id}
                    onChange={() => setSelection(id)}
                  />
                  {label}
                </label>
              ))}
              {selection === "payload" && (
                <label className="radio">
                  <input type="radio" name="selection" checked readOnly />
                  Lead handed over from the lead drawer
                </label>
              )}
            </div>

            {selection === "rows" && (
              <div style={{ marginTop: ".75rem" }}>
                <label htmlFor="rows">Row numbers or ranges</label>
                <input
                  id="rows"
                  value={rows}
                  placeholder="1-25, 40, 55-60"
                  onChange={(e) => setRows(e.target.value)}
                />
              </div>
            )}

            {selection === "tiers" && (
              <div className="checks" style={{ marginTop: ".75rem" }}>
                {["A", "B", "C", "D"].map((tier) => (
                  <label key={tier} className="check">
                    <input
                      type="checkbox"
                      checked={tiers.includes(tier)}
                      onChange={() =>
                        setTiers((prev) =>
                          prev.includes(tier) ? prev.filter((t) => t !== tier) : [...prev, tier])
                      }
                    />
                    Tier {tier}
                  </label>
                ))}
              </div>
            )}

            {selection === "statuses" && (
              <div className="checks" style={{ marginTop: ".75rem" }}>
                {["new", "enriched", "scored", "sent"].map((status) => (
                  <label key={status} className="check">
                    <input
                      type="checkbox"
                      checked={statuses.includes(status)}
                      onChange={() =>
                        setStatuses((prev) =>
                          prev.includes(status)
                            ? prev.filter((s) => s !== status)
                            : [...prev, status])
                      }
                    />
                    {status}
                  </label>
                ))}
              </div>
            )}

            {selection !== "payload" && selection !== "rows" && (
              <p className="state muted">
                Server-side lead selection is not wired to the process API yet —
                it takes an explicit payload. Paste the leads below, or open a
                lead and use “Re-process this lead”.
              </p>
            )}
          </>
        ))}

        {panel("context", "Apply context to selected leads", "not configured", (
          <div className="empty">
            <strong>No context presets</strong>
            Outbound context lives in Settings → Outbound context settings.
          </div>
        ))}

        {panel("visitors", "Website Visitor Accounts", "not configured", (
          <div className="empty">
            <strong>No visitor source connected</strong>
            There is no website-visitor integration on the API yet.
          </div>
        ))}
      </div>

      <Section title="Choose actions to run">
        <Card hint="Actions run in order: Enrichment → Field service fit → Scoring → Email generation → Push to Instantly.">
          <div className="row" style={{ marginTop: 0 }}>
            <button type="button" className="ghost" onClick={() => applyPreset("all")}>Run all</button>
            <button type="button" className="ghost" onClick={() => applyPreset("score_only")}>Score only</button>
            <button type="button" className="ghost" onClick={() => applyPreset("enrich_score")}>Enrich and score</button>
            <button type="button" className="ghost" onClick={() => applyPreset("email_only")}>Generate email only</button>
            <button type="button" className="ghost" onClick={() => applyPreset("missing_emails")}>Generate missing emails only</button>
          </div>

          <div className="checks" style={{ marginTop: "1rem" }}>
            {ACTIONS.map(([id, label]) => (
              <label key={id} className="check">
                <input
                  type="checkbox"
                  checked={actions.has(id)}
                  disabled={id === "push"}
                  onChange={() => toggleAction(id)}
                />
                {label}
                {id === "push" && (
                  <span className="muted"> — never available from the API</span>
                )}
              </label>
            ))}
          </div>
        </Card>
      </Section>

      <Section title="Leads payload">
        <Card hint="The process API takes an explicit list. sync must finish inside the 60s function timeout — use async for anything larger.">
          <form onSubmit={run}>
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
            <div className="row">
              <button type="submit" disabled={busy || !authed}>
                {busy ? "Running…" : "Run pipeline"}
              </button>
              <button type="button" className="ghost" onClick={drain} disabled={!authed}>
                Drain queued
              </button>
            </div>
          </form>
          {result && <pre className="out">{JSON.stringify(result, null, 2)}</pre>}
        </Card>
      </Section>

      <Section title="Run status">
        <Card>
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
              <button type="button" onClick={() => checkRun()} disabled={!authed}>Check</button>
            </div>
          </div>
          {runResult && (
            <pre className="out">
              {runResult.loading ? "Loading…" : JSON.stringify(runResult, null, 2)}
            </pre>
          )}

          <h4>Recent runs</h4>
          <AsyncState
            loading={runs.loading}
            error={runs.error}
            empty={!runs.loading && (runs.data?.runs || []).length === 0}
            emptyTitle="No runs yet"
            emptyText="Submit one above."
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
      </Section>
    </>
  );
}
