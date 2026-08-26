import { formatDate, useApi } from "../../lib/api";
import { AsyncState, Card, PageHead, RunStatus, Section } from "../common";

export default function Prompts({ apiKey }) {
  const { data, loading, error, reload } = useApi(
    "/api/v1/prompts",
    apiKey,
    { skip: !apiKey },
  );

  const configs = data?.configs || [];
  const recommendations = data?.recommendations || [];
  const winners = data?.winners || [];

  return (
    <>
      <PageHead
        title="Prompts"
        note="Prompt overrides, self-improvement recommendations, and the winning examples the generator draws from."
      />

      <Section
        title="Prompt overrides"
        note="Per-channel overlays on the built-in system prompts."
        actions={<button type="button" className="ghost" onClick={reload}>Refresh</button>}
      >
        <Card>
          <AsyncState
            loading={loading}
            error={error}
            empty={!loading && configs.length === 0}
            emptyTitle="No overrides"
            emptyText="The generator is using the built-in prompts for every channel."
          >
            <div className="content-list">
              {configs.map((config) => (
                <article key={config.id} className="content-card">
                  <div className="content-meta">
                    <span className="pill-sm">{config.channel || "all channels"}</span>
                    {config.is_active
                      ? <span className="pill-sm ok">active</span>
                      : <span className="pill-sm">inactive</span>}
                    {config.prompt_version && (
                      <span className="muted">{config.prompt_version}</span>
                    )}
                    <span className="spacer" />
                    <span className="muted">
                      {config.length} chars · {formatDate(config.updated_at)}
                      {config.updated_by ? ` · ${config.updated_by}` : ""}
                    </span>
                  </div>
                  <pre className="content-body">{config.preview}</pre>
                </article>
              ))}
            </div>
          </AsyncState>
        </Card>
      </Section>

      <Section
        title="Recommendations"
        note="Produced by the self-improvement loop. Every change is gated on human approval."
      >
        <Card>
          <AsyncState
            loading={loading}
            error={error}
            empty={!loading && recommendations.length === 0}
            emptyTitle="No recommendations"
            emptyText="The loop needs engagement data before it proposes a change."
          >
            <div className="content-list">
              {recommendations.map((rec) => (
                <article key={rec.id} className="content-card">
                  <div className="content-meta">
                    <span className="pill-sm">{rec.channel || "email"}</span>
                    {rec.bottleneck && <span className="pill-sm">{rec.bottleneck}</span>}
                    <RunStatus status={rec.status || rec.loop_status} />
                    {rec.risk_level && (
                      <span className={`pill-sm ${rec.risk_level === "high" ? "err" : ""}`}>
                        risk: {rec.risk_level}
                      </span>
                    )}
                    {rec.low_confidence && <span className="pill-sm warn">low confidence</span>}
                    <span className="spacer" />
                    <span className="muted">{formatDate(rec.created_at)}</span>
                  </div>
                  {rec.diagnosis && <p className="content-subject">{rec.diagnosis}</p>}
                  {rec.recommended_change && <p className="rationale">{rec.recommended_change}</p>}
                  <div className="content-foot">
                    {rec.expected_impact && <span>expected: {rec.expected_impact}</span>}
                    {rec.sample_size != null && <span>· n={rec.sample_size}</span>}
                    {rec.confidence && <span>· {rec.confidence}</span>}
                  </div>
                </article>
              ))}
            </div>
          </AsyncState>
        </Card>
      </Section>

      <Section title="Winning examples" note="High-performing copy the generator uses as few-shot examples.">
        <Card>
          <AsyncState
            loading={loading}
            error={error}
            empty={!loading && winners.length === 0}
            emptyTitle="No winners yet"
            emptyText="Promote a high-performing email to seed the library."
          >
            <div className="content-list">
              {winners.map((winner) => (
                <article key={winner.id} className="content-card">
                  <div className="content-meta">
                    <span className="pill-sm">{winner.content_type || "email"}</span>
                    {winner.manually_flagged && <span className="pill-sm ok">hand-picked</span>}
                    {winner.reply_rate != null && (
                      <span className="muted">reply rate {winner.reply_rate}</span>
                    )}
                    <span className="spacer" />
                    <span className="muted">{formatDate(winner.promoted_at)}</span>
                  </div>
                  {winner.subject && <p className="content-subject">{winner.subject}</p>}
                  <pre className="content-body">{winner.body}</pre>
                </article>
              ))}
            </div>
          </AsyncState>
        </Card>
      </Section>
    </>
  );
}
