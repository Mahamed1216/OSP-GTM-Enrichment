import { formatDate, useApi } from "../../lib/api";
import { AsyncState, Card, PageHead, Strength, Tier } from "../common";

export default function SignalFeed({ apiKey, onOpenLead }) {
  const { data, loading, error, reload } = useApi(
    "/api/v1/signals?limit=100",
    apiKey,
    { skip: !apiKey },
  );
  const signals = data?.signals || [];

  return (
    <>
      <PageHead
        title="Signal Feed"
        note="Buying-intent signals found for leads — hiring activity and signals imported with the source payload."
      />

      <Card
        title={`${signals.length} signal${signals.length === 1 ? "" : "s"}`}
        actions={<button type="button" className="ghost" onClick={reload}>Refresh</button>}
      >
        <AsyncState
          loading={loading}
          error={error}
          empty={!loading && signals.length === 0}
          emptyTitle="No signals captured"
          emptyText="Signals appear after a hiring-signal run or an import that carried source signals."
        >
          <div className="content-list">
            {signals.map((signal) => (
              <article key={signal.id} className="content-card">
                <div className="content-meta">
                  <button
                    type="button"
                    className="linkish"
                    onClick={() => onOpenLead(signal.lead_id)}
                  >
                    {signal.lead_name || `Lead #${signal.lead_id}`}
                  </button>
                  {signal.company && <span className="muted">· {signal.company}</span>}
                  <span className="pill-sm">{signal.signal_type}</span>
                  <Strength value={signal.strength} />
                  {signal.base_tier && (
                    <span className="muted">
                      base <Tier value={signal.base_tier} />
                    </span>
                  )}
                  <span className="spacer" />
                  <span className="muted">{formatDate(signal.updated_at)}</span>
                </div>

                {signal.found ? (
                  <>
                    {signal.summary && <p className="content-subject">{signal.summary}</p>}
                    {signal.why_it_matters && (
                      <p className="rationale">{signal.why_it_matters}</p>
                    )}
                    {signal.roles.length > 0 && (
                      <ul className="chips">
                        {signal.roles.map((role, i) => <li key={i}>{role}</li>)}
                      </ul>
                    )}
                    <div className="content-foot">
                      {signal.recency && <span>recency: {signal.recency}</span>}
                      {signal.uplift && signal.uplift !== "none" && (
                        <span>
                          · uplift {signal.uplift.replace("_to_", " → ")}
                          {signal.applied_uplift ? " (applied)" : " (not applied)"}
                        </span>
                      )}
                      {signal.source_urls.length > 0 && (
                        <span>· {signal.source_urls.length} source(s)</span>
                      )}
                    </div>
                  </>
                ) : (
                  <p className="state muted">
                    Ran, no relevant signal found
                    {signal.status ? ` (${signal.status})` : ""}.
                  </p>
                )}
                {signal.error && <p className="state err">{signal.error}</p>}
              </article>
            ))}
          </div>
        </AsyncState>
      </Card>
    </>
  );
}
