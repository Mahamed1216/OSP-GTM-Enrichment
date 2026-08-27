import { useApi } from "../../lib/api";
import { AsyncState, Card, Metric, PageHead, Section, TierChip } from "../common";

/**
 * Expansion / re-engagement view.
 *
 * There is no expansion model in the backend yet, so rather than invent one this
 * derives the two account views the current schema can honestly support:
 * companies with more than one lead (an account, not a contact), and replied
 * leads worth re-engaging. Everything else is an explicit empty state.
 */
export default function ClientExpansion({ authed, onOpenLead }) {
  const leads = useApi("/api/v1/leads?limit=200", { skip: !authed });
  const engagement = useApi("/api/v1/engagement?limit=50", { skip: !authed });

  const rows = leads.data?.leads || [];
  const replies = engagement.data?.replies || [];

  // Group by company: more than one contact means there is an account to expand.
  const byCompany = new Map();
  for (const row of rows) {
    const key = (row.Company || "").trim();
    if (!key) continue;
    if (!byCompany.has(key)) byCompany.set(key, []);
    byCompany.get(key).push(row);
  }
  const accounts = [...byCompany.entries()]
    .map(([company, contacts]) => ({
      company,
      contacts,
      sent: contacts.filter((c) => c.Sent).length,
      replied: contacts.filter((c) => c.Replied).length,
      best: contacts.map((c) => c.Tier).filter(Boolean).sort()[0] || null,
    }))
    .filter((account) => account.contacts.length > 1)
    .sort((a, b) => b.contacts.length - a.contacts.length);

  const engaged = accounts.filter((a) => a.replied > 0).length;

  return (
    <>
      <PageHead
        title="Client Expansion"
        note="Accounts with more than one contact, and replies worth re-engaging."
      />

      <Section title="Accounts">
        <div className="metrics">
          <Metric label="Multi-contact accounts" value={accounts.length} accent />
          <Metric label="With a reply" value={engaged} />
          <Metric label="Contacts in view" value={rows.length} />
        </div>
      </Section>

      <Section title="Multi-contact accounts" note="More than one lead at the same company.">
        <Card>
          <AsyncState
            loading={leads.loading}
            error={leads.error}
            empty={!leads.loading && accounts.length === 0}
            emptyTitle="No expansion accounts yet"
            emptyText="An account appears once two or more contacts from the same company are in the workspace."
          >
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr><th>Company</th><th>Contacts</th><th>Best tier</th><th>Sent</th><th>Replied</th><th>Names</th></tr>
                </thead>
                <tbody>
                  {accounts.map((account) => (
                    <tr key={account.company}>
                      <td><strong>{account.company}</strong></td>
                      <td>{account.contacts.length}</td>
                      <td><TierChip value={account.best} /></td>
                      <td>{account.sent}</td>
                      <td>{account.replied}</td>
                      <td className="muted">
                        {account.contacts.slice(0, 4).map((contact, i) => (
                          <span key={contact.id}>
                            {i > 0 && ", "}
                            <button type="button" className="linkish" onClick={() => onOpenLead(contact.id)}>
                              {contact.Name || `#${contact.id}`}
                            </button>
                          </span>
                        ))}
                        {account.contacts.length > 4 && ` +${account.contacts.length - 4}`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </AsyncState>
        </Card>
      </Section>

      <Section title="Re-engagement" note="Leads that replied — the warmest expansion path.">
        <Card>
          <AsyncState
            loading={engagement.loading}
            error={engagement.error}
            empty={!engagement.loading && replies.length === 0}
            emptyTitle="No replies to re-engage"
            emptyText="Replies captured by the Instantly webhook show up here."
          >
            <ul className="plain">
              {replies.map((reply) => (
                <li key={reply.id}>
                  {reply.lead_id ? (
                    <button type="button" className="linkish" onClick={() => onOpenLead(reply.lead_id)}>
                      {reply.prospect_name || `Lead #${reply.lead_id}`}
                    </button>
                  ) : (
                    <strong>{reply.prospect_name || "Unknown"}</strong>
                  )}
                  {reply.company && <span className="muted"> · {reply.company}</span>}
                  {reply.classification && <span className="pill-sm"> {reply.classification}</span>}
                </li>
              ))}
            </ul>
          </AsyncState>
        </Card>
      </Section>
    </>
  );
}
