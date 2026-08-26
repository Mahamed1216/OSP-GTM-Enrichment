import { useApi } from "../../lib/api";
import { AsyncState, Card, KV, PageHead, Tier } from "../common";

const REQUIRED = ["DATABASE_URL", "INTERNAL_API_KEY"];

const PURPOSE = {
  DATABASE_URL: "Supabase Postgres connection (pooler URI)",
  INTERNAL_API_KEY: "bearer auth for /api/v1/*",
  ANTHROPIC_API_KEY: "scoring + content generation",
  APIFY_API_TOKEN: "LinkedIn enrichment",
  TAVILY_API_KEY: "buyer research and hiring signals",
  INSTANTLY_API_KEY: "Instantly reads and pushes",
  INSTANTLY_CAMPAIGN_ID: "default campaign when a workspace has none",
  INSTANTLY_WEBHOOK_SECRET: "validates the reply webhook",
  LEAD_SOURCE_JOB_SECRET: "validates the scheduler endpoint",
  CRON_SECRET: "lets a Vercel Cron drain queued runs",
};

export default function Settings({ apiKey, health }) {
  const { data, loading, error, reload } = useApi(
    "/api/v1/settings/status",
    apiKey,
    { skip: !apiKey },
  );

  const env = data?.env || {};
  const scoring = data?.scoring || {};

  return (
    <>
      <PageHead title="Settings" note="Configuration presence, scoring thresholds and machine endpoints. No secret value is ever sent to the browser." />
      <Card
        title="Configuration"
        hint="Presence only — no value is ever sent to the browser."
        actions={<button type="button" className="ghost" onClick={reload}>Refresh</button>}
      >
        <AsyncState loading={loading} error={error}>
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr><th>Variable</th><th>Status</th><th>Used for</th></tr>
              </thead>
              <tbody>
                {Object.entries(env).map(([name, present]) => {
                  const required = REQUIRED.includes(name);
                  return (
                    <tr key={name}>
                      <td className="mono">{name}{required && <span className="req"> *</span>}</td>
                      <td>
                        {present ? (
                          <span className="pill-sm ok">configured</span>
                        ) : (
                          <span className={`pill-sm ${required ? "err" : ""}`}>
                            {required ? "missing" : "not set"}
                          </span>
                        )}
                      </td>
                      <td className="muted">{PURPOSE[name] || "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="hint">* required — the API cannot serve data without it.</p>
        </AsyncState>
      </Card>

      <div className="grid">
        <Card title="Runtime" hint="Reported by the public health endpoint.">
          <KV rows={[
            ["Service", health.data?.service],
            ["API status", health.data?.status],
            ["Database", health.data?.database_configured ? "connected" : "not configured"],
            ["Pipeline code", health.data?.backend_importable ? "importable" : "import failed"],
            ["Error", health.data?.backend_error || health.data?.database_error || null],
          ]} />
        </Card>

        <Card title="Scoring" hint="Thresholds the pipeline scores against.">
          <AsyncState loading={loading} error={error}>
            <KV rows={[
              ["Email verifier", scoring.email_verifier],
              ["Tier A minimum", scoring.tier_a_min],
              ["Tier B minimum", scoring.tier_b_min],
              ["Send minimum tier", scoring.send_min_tier ? <Tier value={scoring.send_min_tier} /> : null],
              ["Scoring model", <span className="mono">{scoring.scoring_model}</span>],
              ["Content model", <span className="mono">{scoring.content_model}</span>],
            ]} />
          </AsyncState>
        </Card>
      </div>

      <Card title="Machine endpoints" hint="Called by Instantly and schedulers, not from this console.">
        <ul className="plain">
          <li><span className="method">POST</span><code>/api/instantly/reply-webhook</code> — header <code>X-Webhook-Secret</code></li>
          <li><span className="method">POST</span><code>/api/lead-source/run-scheduled</code> — header <code>X-Job-Secret</code></li>
          <li><span className="method">POST</span><code>/api/v1/drain</code> — bearer key or <code>CRON_SECRET</code></li>
          <li><span className="method">GET</span><code>/health</code> — public</li>
        </ul>
      </Card>
    </>
  );
}
