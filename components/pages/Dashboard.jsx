import Link from "next/link";
import { useState } from "react";

import { formatDate, useApi } from "../../lib/api";
import { AsyncState, Card, Metric, RunStatus, Section, TierChip } from "../common";

const TIERS = ["A", "B", "C", "D"];
const CALL_TABS = [["daily", "Daily"], ["weekly", "Weekly"], ["pipeline", "Pipeline"]];

export default function Dashboard({ apiKey, health, onOpenLead }) {
  const [callTab, setCallTab] = useState("daily");

  const summary = useApi("/api/v1/dashboard/summary", apiKey, { skip: !apiKey });
  const content = useApi("/api/v1/generated-content?limit=5", apiKey, { skip: !apiKey });
  const engagement = useApi("/api/v1/engagement?limit=5", apiKey, { skip: !apiKey });
  // Phone-ready leads: scored A/B and not yet sent.
  const calls = useApi(
    "/api/v1/leads?tier=A,B&not_sent_only=true&limit=8",
    apiKey,
    { skip: !apiKey },
  );

  const counts = summary.data?.counts || {};
  const tiers = summary.data?.tiers || {};
  const runs = summary.data?.recent_runs || [];
  const failed = summary.data?.failed_runs || [];
  const replies = engagement.data?.replies || [];

  const scored = TIERS.reduce((total, tier) => total + (tiers[tier] || 0), 0);
  const unscored = Math.max(0, (counts.leads_total || 0) - scored);

  return (
    <>
      <div className="hero">
        <h1>Outbound that lands.</h1>
        <p>
          Lead enrichment, scoring, and personalized outreach. Every signal
          cited.
        </p>
      </div>

      <Section title="Pipeline" note="Totals across the current workspace.">
        <AsyncState loading={summary.loading} error={summary.error}>
          <div className="metrics">
            <Metric label="Leads" value={counts.leads_total ?? 0} accent />
            <Metric label="Enriched" value={counts.enriched ?? 0} />
            <Metric label="Scored" value={counts.scored ?? 0} />
            <Metric label="Sent" value={counts.sent ?? 0} />
            <Metric label="Replies" value={counts.replied ?? 0} />
          </div>
        </AsyncState>
      </Section>

      <Section title="Score tiers" note="Assigned by the scorer; unscored leads have not run yet.">
        <AsyncState loading={summary.loading} error={summary.error}>
          <div className="metrics">
            {TIERS.map((tier) => (
              <Metric
                key={tier}
                label={`Tier ${tier}`}
                value={tiers[tier] ?? 0}
                chip={<TierChip value={tier} />}
              />
            ))}
            <Metric label="Unscored" value={unscored} chip={<TierChip value={null} />} />
          </div>
        </AsyncState>
      </Section>

      <Section title="Apollo Autopilot (this week)">
        <Card
          actions={
            <Link href="/apollo-autopilot" className="btn-ghost">
              Open settings
            </Link>
          }
        >
          <p className="state">
            Apollo Autopilot is off. Enable it to source weekly lead batches.
          </p>
          <p className="hint">
            No sourcing runs are scheduled. Leads currently arrive from CSV
            import, the lead-source API, or a manual run.
          </p>
        </Card>
      </Section>

      <Section
        title="Daily Calls"
        note="Scored A/B leads that have not been emailed yet."
        actions={
          <div className="subtabs">
            {CALL_TABS.map(([id, label]) => (
              <button
                key={id}
                type="button"
                className={callTab === id ? "subtab active" : "subtab"}
                onClick={() => setCallTab(id)}
              >
                {label}
              </button>
            ))}
          </div>
        }
      >
        <Card>
          {callTab !== "daily" ? (
            <div className="empty">
              <strong>{callTab === "weekly" ? "Weekly view" : "Pipeline view"}</strong>
              Not built yet — the daily list is driven by live lead data.
            </div>
          ) : (
            <AsyncState
              loading={calls.loading}
              error={calls.error}
              empty={!calls.loading && (calls.data?.leads || []).length === 0}
              emptyTitle="No call-ready leads"
              emptyText="Leads appear here once they are scored A or B and not yet emailed."
            >
              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr><th>Name</th><th>Title</th><th>Company</th><th>Tier</th><th>Score</th></tr>
                  </thead>
                  <tbody>
                    {(calls.data?.leads || []).map((row) => (
                      <tr key={row.id} className="clickable" onClick={() => onOpenLead(row.id)}>
                        <td>{row.Name || "—"}</td>
                        <td className="muted">{row.Title || "—"}</td>
                        <td>{row.Company || "—"}</td>
                        <td><TierChip value={row.Tier} /></td>
                        <td>{row.Score ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </AsyncState>
          )}
        </Card>
      </Section>

      <Section title="Recent activity">
        <div className="grid-2">
          <Card title="Recent runs" hint="Processing jobs submitted through the API.">
            <AsyncState
              loading={summary.loading}
              error={summary.error}
              empty={!summary.loading && runs.length === 0}
              emptyTitle="No runs yet"
              emptyText="Submit one from Run Pipeline."
            >
              <table className="table">
                <thead><tr><th>Run</th><th>Status</th><th>Leads</th><th>Created</th></tr></thead>
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
          </Card>

          <Card title="Failed jobs" hint="Runs that ended in a failed state.">
            <AsyncState
              loading={summary.loading}
              error={summary.error}
              empty={!summary.loading && failed.length === 0}
              emptyTitle="No failures"
              emptyText="Every recent run completed."
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
          </Card>

          <Card title="Recently generated emails">
            <AsyncState
              loading={content.loading}
              error={content.error}
              empty={!content.loading && (content.data?.content || []).length === 0}
              emptyTitle="No content yet"
              emptyText="Generated emails appear here after a pipeline run."
            >
              <table className="table">
                <thead><tr><th>Lead</th><th>Subject</th><th>Created</th></tr></thead>
                <tbody>
                  {(content.data?.content || []).map((item) => (
                    <tr key={item.id} className="clickable" onClick={() => onOpenLead(item.lead_id)}>
                      <td>{item.lead_name || `#${item.lead_id}`}</td>
                      <td>{item.subject || <span className="muted">no subject</span>}</td>
                      <td className="muted">{formatDate(item.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </AsyncState>
          </Card>

          <Card title="Recent replies" hint="Inbound replies synced from Instantly.">
            <AsyncState
              loading={engagement.loading}
              error={engagement.error}
              empty={!engagement.loading && replies.length === 0}
              emptyTitle="No replies yet"
              emptyText="Replies arrive through the Instantly webhook."
            >
              <ul className="plain">
                {replies.map((reply) => (
                  <li key={reply.id}>
                    <strong>{reply.prospect_name || "Unknown"}</strong>
                    {reply.company && <span className="muted"> · {reply.company}</span>}{" "}
                    {reply.classification && <span className="pill-sm">{reply.classification}</span>}
                    <div className="muted">{formatDate(reply.received_at)}</div>
                  </li>
                ))}
              </ul>
            </AsyncState>
          </Card>
        </div>
      </Section>

      <Section title="Service">
        <div className="metrics">
          <Metric
            label="API"
            value={health.data?.status || (health.loading ? "…" : "down")}
          />
          <Metric
            label="Database"
            value={health.data?.database_configured ? "connected" : "not set"}
          />
          <Metric
            label="Pipeline code"
            value={health.data?.backend_importable ? "loaded" : "failed"}
          />
        </div>
        {(health.data?.backend_error || health.data?.database_error) && (
          <p className="state err">
            {health.data.backend_error || health.data.database_error}
          </p>
        )}
      </Section>
    </>
  );
}
