import { formatDate, useApi } from "../lib/api";
import { AsyncState, Bool, KV, RunStatus, Tier } from "./common";

/** Slide-over detail for one lead: contact, signals, enrichment, score, content. */
export default function LeadDetail({ leadId, apiKey, onClose, onAction }) {
  const { data, loading, error, reload } = useApi(
    leadId ? `/api/v1/leads/${leadId}` : null,
    apiKey,
    { skip: !leadId || !apiKey },
  );

  const lead = data?.lead || {};
  const score = data?.score;
  const enrichment = data?.enrichment;
  const contents = data?.contents || [];
  const engagements = data?.engagements || [];
  const hiring = data?.hiring_signal;
  const source = data?.source_signal;

  const name = [lead.first_name, lead.last_name].filter(Boolean).join(" ");

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <header className="drawer-head">
          <div>
            <h2>{name || `Lead #${leadId}`}</h2>
            <p className="hint">{lead.title || "—"}{lead.company ? ` · ${lead.company}` : ""}</p>
          </div>
          <button type="button" className="ghost" onClick={onClose}>Close</button>
        </header>

        <AsyncState loading={loading} error={error}>
          <div className="drawer-body">
            <section>
              <h3>Contact</h3>
              <KV rows={[
                ["Email", <span className="mono">{lead.email}</span>],
                ["Verified", lead.email_verification_status
                  ? `${lead.email_verification_status}${lead.email_verification_provider ? ` (${lead.email_verification_provider})` : ""}`
                  : null],
                ["LinkedIn", lead.linkedin_url
                  ? <a href={lead.linkedin_url} target="_blank" rel="noreferrer">profile</a>
                  : null],
                ["Added", formatDate(lead.created_at)],
              ]} />
            </section>

            <section>
              <h3>Company</h3>
              <KV rows={[
                ["Company", lead.company],
                ["Domain", lead.company_domain],
                ["Industry", lead.industry],
                ["Company page", lead.company_linkedin_url
                  ? <a href={lead.company_linkedin_url} target="_blank" rel="noreferrer">LinkedIn</a>
                  : null],
              ]} />
            </section>

            <section>
              <h3>Source signals</h3>
              {source || hiring || lead.external_source ? (
                <KV rows={[
                  ["Source", lead.external_source],
                  ["Client", lead.external_client_slug],
                  ["Source tier", lead.source_tier ? <Tier value={lead.source_tier} /> : null],
                  ["Source score", lead.source_tier_score],
                  ["Hiring signal", hiring
                    ? `${hiring.strength || "—"}${hiring.summary ? ` — ${hiring.summary}` : ""}`
                    : null],
                  ["Imported signal", source
                    ? `${source.strength || "—"}${source.summary ? ` — ${source.summary}` : ""}`
                    : null],
                ]} />
              ) : (
                <p className="state muted">No source signals recorded.</p>
              )}
            </section>

            <section>
              <h3>Enrichment</h3>
              {enrichment ? (
                <KV rows={[
                  ["Enriched at", formatDate(enrichment.enriched_at)],
                  ["LinkedIn profile", <Bool value={!!enrichment.linkedin_profile} yes="captured" no="none" />],
                  ["Company details", <Bool value={!!enrichment.company_details} yes="captured" no="none" />],
                  ["Company news", <Bool value={!!enrichment.company_news} yes="captured" no="none" />],
                  ["Buyer research", <Bool value={!!enrichment.buyer_accounts} yes="captured" no="none" />],
                ]} />
              ) : (
                <p className="state muted">Not enriched yet.</p>
              )}
            </section>

            <section>
              <h3>Score</h3>
              {score ? (
                <>
                  <div className="score-head">
                    <Tier value={score.tier} />
                    <span className="score-num">{score.score}</span>
                    <span className="muted">{score.model}</span>
                  </div>
                  <p className="rationale">{score.rationale || "No rationale recorded."}</p>
                  {(score.signals_used || []).length > 0 && (
                    <ul className="chips">
                      {score.signals_used.map((s, i) => <li key={i}>{s}</li>)}
                    </ul>
                  )}
                </>
              ) : (
                <p className="state muted">Not scored yet.</p>
              )}
            </section>

            <section>
              <h3>Generated content</h3>
              {contents.length === 0 ? (
                <p className="state muted">Nothing generated yet.</p>
              ) : (
                contents.map((item) => (
                  <article key={item.id} className="content-card">
                    <div className="content-meta">
                      <span className="pill-sm">{item.kind}</span>
                      {item.delivery_status && <RunStatus status={item.delivery_status} />}
                      <span className="muted">{formatDate(item.created_at)}</span>
                      {item.prompt_version && <span className="muted">· {item.prompt_version}</span>}
                    </div>
                    {item.subject && <p className="content-subject">{item.subject}</p>}
                    <pre className="content-body">{item.body}</pre>
                    {item.skip_reason && (
                      <p className="state err">Blocked: {item.skip_reason}</p>
                    )}
                    {item.error_message && (
                      <p className="state err">{item.error_message}</p>
                    )}
                  </article>
                ))
              )}
            </section>

            <section>
              <h3>Engagement</h3>
              {engagements.length === 0 ? (
                <p className="state muted">No engagement events.</p>
              ) : (
                <ul className="plain">
                  {engagements.map((e, i) => (
                    <li key={i}>
                      <span className="pill-sm">{e.event || e.status || "event"}</span>{" "}
                      <span className="muted">{formatDate(e.occurred_at || e.created_at)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section>
              <h3>Actions</h3>
              <p className="hint">
                Re-processing runs the full pipeline for this lead: enrichment,
                scoring and content. It never sends email.
              </p>
              <div className="row">
                <button
                  type="button"
                  onClick={() => onAction(lead)}
                  disabled={!lead.email}
                >
                  Re-process this lead
                </button>
                <button type="button" className="ghost" onClick={reload}>
                  Refresh
                </button>
              </div>
            </section>
          </div>
        </AsyncState>
      </aside>
    </div>
  );
}
