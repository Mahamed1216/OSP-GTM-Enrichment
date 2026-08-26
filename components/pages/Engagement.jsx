import { formatDate, useApi } from "../../lib/api";
import { AsyncState, Bool, Card, KV, Metric, PageHead, Section } from "../common";
import Content from "./Content";

export default function Engagement({ apiKey, onOpenLead }) {
  const { data, loading, error, reload } = useApi(
    "/api/v1/engagement?limit=50",
    apiKey,
    { skip: !apiKey },
  );

  const counts = data?.counts || {};
  const campaign = data?.campaign;
  const events = data?.events || [];
  const replies = data?.replies || [];

  return (
    <>
      <PageHead
        title="Engagement"
        note="Delivery and reply activity synced back from Instantly."
      />

      <Section title="Delivery">
        <AsyncState loading={loading} error={error}>
          <div className="metrics">
            <Metric label="Sent" value={counts.sent ?? 0} accent />
            <Metric label="Opened" value={counts.opened ?? 0} />
            <Metric label="Clicked" value={counts.clicked ?? 0} />
            <Metric label="Replied" value={counts.replied ?? 0} />
            <Metric label="Bounced" value={counts.bounced ?? 0} />
          </div>
        </AsyncState>
      </Section>

      {campaign && (
        <Section title="Campaign snapshot" note={`Last synced ${formatDate(campaign.synced_at)}`}>
          <Card>
            <KV rows={[
              ["Campaign", <span className="mono">{campaign.campaign_id}</span>],
              ["Leads", campaign.leads_count],
              ["Contacted", campaign.contacted_count],
              ["Emails sent", campaign.emails_sent_count],
              ["Opens", campaign.open_count],
              ["Replies", campaign.reply_count],
              ["Positive replies", campaign.positive_reply_count],
              ["Bounced", campaign.bounced_count],
            ]} />
          </Card>
        </Section>
      )}

      <Section title="Replies" note="Inbound replies captured by the Instantly webhook.">
        <Card actions={<button type="button" className="ghost" onClick={reload}>Refresh</button>}>
          <AsyncState
            loading={loading}
            error={error}
            empty={!loading && replies.length === 0}
            emptyTitle="No replies yet"
            emptyText="Replies arrive through POST /api/instantly/reply-webhook."
          >
            <div className="content-list">
              {replies.map((reply) => (
                <article key={reply.id} className="content-card">
                  <div className="content-meta">
                    {reply.lead_id ? (
                      <button type="button" className="linkish" onClick={() => onOpenLead(reply.lead_id)}>
                        {reply.prospect_name || `Lead #${reply.lead_id}`}
                      </button>
                    ) : (
                      <strong>{reply.prospect_name || "Unknown"}</strong>
                    )}
                    {reply.company && <span className="muted">· {reply.company}</span>}
                    {reply.classification && <span className="pill-sm">{reply.classification}</span>}
                    {reply.status && <span className="pill-sm">{reply.status}</span>}
                    <span className="spacer" />
                    <span className="muted">{formatDate(reply.received_at)}</span>
                  </div>
                  {reply.reply_text && <pre className="content-body">{reply.reply_text}</pre>}
                  {reply.recommended_action && (
                    <p className="hint">Recommended: {reply.recommended_action}</p>
                  )}
                </article>
              ))}
            </div>
          </AsyncState>
        </Card>
      </Section>

      <Section title="Per-email events">
        <Card>
          <AsyncState
            loading={loading}
            error={error}
            empty={!loading && events.length === 0}
            emptyTitle="No engagement events"
            emptyText="Events appear after the first analytics sync."
          >
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>Lead</th><th>Company</th><th>Subject</th>
                    <th>Sent</th><th>Opened</th><th>Clicked</th><th>Replied</th><th>Bounced</th><th>Synced</th>
                  </tr>
                </thead>
                <tbody>
                  {events.map((event, i) => (
                    <tr key={i} className="clickable" onClick={() => onOpenLead(event.lead_id)}>
                      <td>{event.lead_name || `#${event.lead_id}`}</td>
                      <td className="muted">{event.company || "—"}</td>
                      <td>{event.subject || <span className="muted">—</span>}</td>
                      <td><Bool value={event.sent} /></td>
                      <td><Bool value={event.opened} /></td>
                      <td><Bool value={event.clicked} /></td>
                      <td><Bool value={event.replied} /></td>
                      <td><Bool value={event.bounced} /></td>
                      <td className="muted">{formatDate(event.synced_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </AsyncState>
        </Card>
      </Section>

      <Section
        title="Outbound content"
        note="What the pipeline generated, with safety and delivery state."
      >
        <Content apiKey={apiKey} onOpenLead={onOpenLead} embedded />
      </Section>
    </>
  );
}
