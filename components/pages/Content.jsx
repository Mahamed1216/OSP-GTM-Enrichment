import { useState } from "react";

import { formatDate, useApi } from "../../lib/api";
import { AsyncState, Card, PageHead, RunStatus } from "../common";

const KINDS = [
  ["email", "Email"],
  ["call_script", "Call script"],
  ["linkedin_msg", "LinkedIn DM"],
];

export default function Content({ authed, onOpenLead, embedded = false }) {
  const [kind, setKind] = useState("email");
  const { data, loading, error, reload } = useApi(
    `/api/v1/generated-content?kind=${kind}&limit=50`,
    { skip: !authed },
  );
  const items = data?.content || [];

  return (
    <>
      {!embedded && (
        <PageHead title="Generated content" note="Outbound copy produced by the pipeline." />
      )}
      <Card
      title="Generated content"
      hint="Outbound copy produced by the pipeline. Nothing here has been sent unless it shows a delivery status."
      actions={<button type="button" className="ghost" onClick={reload}>Refresh</button>}
    >
      <div className="filters">
        <div>
          <label htmlFor="kind">Type</label>
          <select id="kind" value={kind} onChange={(e) => setKind(e.target.value)}>
            {KINDS.map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </div>
      </div>

      <AsyncState
        loading={loading}
        error={error}
        empty={!loading && items.length === 0}
        emptyText={`No ${kind.replace("_", " ")} content generated yet.`}
      >
        <div className="content-list">
          {items.map((item) => (
            <article key={item.id} className="content-card">
              <div className="content-meta">
                <button
                  type="button"
                  className="linkish"
                  onClick={() => onOpenLead(item.lead_id)}
                >
                  {item.lead_name || `Lead #${item.lead_id}`}
                </button>
                {item.company && <span className="muted">· {item.company}</span>}
                <span className="spacer" />
                {item.skip_reason ? (
                  <span className="pill-sm err">blocked</span>
                ) : item.delivery_status ? (
                  <RunStatus status={item.delivery_status} />
                ) : (
                  <span className="pill-sm">draft</span>
                )}
                <span className="muted">{formatDate(item.created_at)}</span>
              </div>
              {item.subject && <p className="content-subject">{item.subject}</p>}
              <pre className="content-body">{item.body}</pre>
              <div className="content-foot muted">
                {item.prompt_version && <span>prompt {item.prompt_version}</span>}
                {item.model && <span>· {item.model}</span>}
                {item.delivered_at && <span>· delivered {formatDate(item.delivered_at)}</span>}
              </div>
              {item.skip_reason && (
                <p className="state err">Blocked before sending: {item.skip_reason}</p>
              )}
              {item.error_message && <p className="state err">{item.error_message}</p>}
            </article>
          ))}
        </div>
      </AsyncState>
    </Card>
    </>
  );
}
