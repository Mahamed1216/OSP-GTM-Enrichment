import { formatDate, useApi } from "../lib/api";
import { AsyncState, Panel, RunStatus, Stat, Tier } from "./common";

const TIERS = ["A", "B", "C", "D"];

export default function Overview({ apiKey, health, onOpenLead }) {
  const summary = useApi("/api/v1/dashboard/summary", apiKey, { skip: !apiKey });
  const content = useApi("/api/v1/generated-content?limit=5", apiKey, { skip: !apiKey });

  const counts = summary.data?.counts || {};
  const tiers = summary.data?.tiers || {};
  const runs = summary.data?.recent_runs || [];
  const failed = summary.data?.failed_runs || [];

  return (
    <>
      <div className="grid">
        <Panel title="Service" hint="Public health check — no key required.">
          <div className="stats">
            <Stat
              label="API"
              value={health.data?.status || (health.loading ? "…" : "unreachable")}
              tone={health.data?.status === "ok" ? "ok" : "err"}
            />
            <Stat
              label="Database"
              value={health.data?.database_configured ? "connected" : "not configured"}
              tone={health.data?.database_configured ? "ok" : "err"}
            />
            <Stat
              label="Pipeline code"
              value={health.data?.backend_importable ? "loaded" : "failed"}
              tone={health.data?.backend_importable ? "ok" : "err"}
            />
          </div>
          {(health.data?.backend_error || health.data?.database_error) && (
            <p className="state err">
              {health.data.backend_error || health.data.database_error}
            </p>
          )}
        </Panel>

        <Panel title="Pipeline" hint="Totals across the current workspace.">
          <AsyncState loading={summary.loading} error={summary.error}>
            <div className="stats">
              <Stat label="Leads" value={counts.leads_total ?? 0} />
              <Stat label="Enriched" value={counts.enriched ?? 0} />
              <Stat label="Scored" value={counts.scored ?? 0} />
              <Stat label="Sent" value={counts.sent ?? 0} />
              <Stat label="Replied" value={counts.replied ?? 0} />
              <Stat label="Ready to send" value={summary.data?.ready_to_send ?? 0} />
            </div>
          </AsyncState>
        </Panel>

        <Panel title="Leads by tier" hint="Tier is assigned by the scorer.">
          <AsyncState loading={summary.loading} error={summary.error}>
            <div className="stats">
              {TIERS.map((t) => (
                <Stat key={t} label={<Tier value={t} />} value={tiers[t] ?? 0} />
              ))}
              <Stat label="Unscored" value={tiers.unscored ?? tiers["—"] ?? 0} />
            </div>
          </AsyncState>
        </Panel>
      </div>

      <div className="grid">
        <Panel title="Recent runs" hint="Processing jobs submitted through the API.">
          <AsyncState
            loading={summary.loading}
            error={summary.error}
            empty={!summary.loading && runs.length === 0}
            emptyText="No runs yet. Submit one from Processing."
          >
            <table className="table">
              <thead>
                <tr>
                  <th>Run</th><th>Status</th><th>Leads</th><th>Created</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.run_id}>
                    <td><code>{run.run_id}</code></td>
                    <td><RunStatus status={run.status} /></td>
                    <td>{run.processed_count ?? 0}/{run.lead_count ?? 0}</td>
                    <td className="muted">{formatDate(run.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </AsyncState>
        </Panel>

        <Panel title="Failed jobs" hint="Runs that ended in a failed state.">
          <AsyncState
            loading={summary.loading}
            error={summary.error}
            empty={!summary.loading && failed.length === 0}
            emptyText="No failed runs."
          >
            <ul className="plain">
              {failed.map((run) => (
                <li key={run.run_id}>
                  <code>{run.run_id}</code>
                  <span className="err"> {run.error || "failed"}</span>
                </li>
              ))}
            </ul>
          </AsyncState>
        </Panel>
      </div>

      <Panel
        title="Recently generated emails"
        hint="Newest outbound content, across all leads."
        wide
      >
        <AsyncState
          loading={content.loading}
          error={content.error}
          empty={!content.loading && (content.data?.content || []).length === 0}
          emptyText="No content generated yet."
        >
          <table className="table">
            <thead>
              <tr>
                <th>Lead</th><th>Company</th><th>Subject</th><th>Created</th>
              </tr>
            </thead>
            <tbody>
              {(content.data?.content || []).map((item) => (
                <tr key={item.id} className="clickable" onClick={() => onOpenLead(item.lead_id)}>
                  <td>{item.lead_name || `#${item.lead_id}`}</td>
                  <td className="muted">{item.company || "—"}</td>
                  <td>{item.subject || <span className="muted">no subject</span>}</td>
                  <td className="muted">{formatDate(item.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </AsyncState>
      </Panel>
    </>
  );
}
