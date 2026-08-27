import { useEffect, useState } from "react";

import { callApi, useApi } from "../../lib/api";
import { AsyncState, Card, KV, PageHead, Section, Tier } from "../common";

const REQUIRED_ENV = ["DATABASE_URL", "ADMIN_PASSWORD"];

const ENV_PURPOSE = {
  DATABASE_URL: "Supabase Postgres connection (pooler URI)",
  ADMIN_PASSWORD: "the password this console signs in with",
  ADMIN_SESSION_SECRET: "signs session cookies (optional; derived from the password if unset)",
  INTERNAL_API_KEY: "internal only — bearer auth for backend-to-backend callers",
  ANTHROPIC_API_KEY: "scoring + content generation",
  APIFY_API_TOKEN: "LinkedIn enrichment",
  TAVILY_API_KEY: "buyer research and hiring signals",
  INSTANTLY_API_KEY: "Instantly reads and pushes",
  INSTANTLY_CAMPAIGN_ID: "default campaign when a workspace has none",
  INSTANTLY_WEBHOOK_SECRET: "validates the reply webhook",
  LEAD_SOURCE_JOB_SECRET: "validates the scheduler endpoint",
  CRON_SECRET: "lets a Vercel Cron drain queued runs",
};

/** Textarea whose value is one item per line, bound to a string[]. */
function ListField({ id, label, value, onChange, placeholder }) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <textarea
        id={id}
        className="list-field"
        value={(value || []).join("\n")}
        placeholder={placeholder}
        spellCheck={false}
        onChange={(e) =>
          onChange(e.target.value.split("\n").map((s) => s.trim()).filter(Boolean))
        }
      />
    </div>
  );
}

function TextField({ id, label, value, onChange, placeholder }) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        value={value || ""}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

export default function Settings({ authed, health }) {
  const status = useApi("/api/v1/settings/status", { skip: !authed });
  const settings = useApi("/api/v1/settings", { skip: !authed });

  const [config, setConfig] = useState(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(null);
  const [open, setOpen] = useState("company");
  const [modal, setModal] = useState(null);
  const [campaignId, setCampaignId] = useState("");
  const [instantlySaved, setInstantlySaved] = useState(null);

  useEffect(() => {
    if (!settings.data) return;
    setConfig(settings.data.config);
    setCampaignId(settings.data.instantly?.campaign_id || "");
    setDirty(false);
  }, [settings.data]);

  const env = status.data?.env || {};
  const db = status.data?.database || {};
  const scoring = status.data?.scoring || {};
  const workspace = settings.data?.workspace || {};
  const instantly = settings.data?.instantly || {};
  const deliverability = settings.data?.deliverability || {};

  function patch(path, value) {
    setConfig((prev) => {
      if (!prev) return prev;
      const next = { ...prev };
      if (path.length === 1) next[path[0]] = value;
      else next[path[0]] = { ...next[path[0]], [path[1]]: value };
      return next;
    });
    setDirty(true);
  }

  async function save() {
    setSaving(true);
    setSaved(null);
    const response = await callApi("/api/v1/settings", {
      method: "POST",
      body: { config },
    });
    setSaving(false);
    if (response.ok) {
      setDirty(false);
      setSaved({ ok: true, message: "Settings saved." });
      settings.reload();
    } else {
      setSaved({ ok: false, message: response.error });
    }
  }

  async function saveInstantly() {
    setInstantlySaved(null);
    const response = await callApi("/api/v1/settings/instantly", {
      method: "POST",
      body: { campaign_id: campaignId },
    });
    setInstantlySaved(
      response.ok
        ? { ok: true, message: "Campaign id saved." }
        : { ok: false, message: response.error },
    );
    if (response.ok) settings.reload();
  }

  const accordion = (id, title, note, children) => (
    <div className="accordion">
      <button
        type="button"
        className="accordion-head"
        aria-expanded={open === id}
        onClick={() => setOpen(open === id ? "" : id)}
      >
        <span className="accordion-caret">{open === id ? "▾" : "▸"}</span>
        <span className="accordion-title">{title}</span>
        {note && <span className="muted accordion-size">{note}</span>}
      </button>
      {open === id && <div className="accordion-body">{children}</div>}
    </div>
  );

  const company = config?.company || {};
  const icp = config?.icp || {};
  const persona = config?.persona || {};
  const signals = config?.signals || {};

  return (
    <>
      <PageHead
        title="Settings."
        note="Outbound configuration for this workspace. No secret value is ever sent to the browser."
      />

      {/* -------------------------------------------------- action cards -- */}
      <div className="grid-2">
        <Card title="Account suppressions">
          <p className="hint">
            Companies removed from active outbound after a definitive no, a
            not-a-fit call, or a do-not-contact request. Review, filter, and
            reactivate accounts.
          </p>
          <div className="row">
            <button type="button" className="ghost" onClick={() => setModal("suppressions")}>
              Manage suppressions
            </button>
          </div>
        </Card>

        <Card title="Calling paused">
          <p className="hint">
            Companies excluded from Daily Calls because a meeting has been set.
            These accounts are engaged, not suppressed. Resume calling when the
            meeting is done.
          </p>
          <div className="row">
            <button type="button" className="ghost" onClick={() => setModal("calling")}>
              Manage calling pauses
            </button>
          </div>
        </Card>

        <Card title="Outbound context settings">
          <p className="hint">
            What each reason-to-contact is worth, how long it stays fresh, and
            what may be said about it.
          </p>
          <div className="row">
            <button type="button" className="ghost" onClick={() => setModal("context")}>
              Outbound Context Settings
            </button>
          </div>
        </Card>

        <Card title="Signal Intelligence">
          <p className="hint">
            Manage signal imports, integration status, and diagnostics.
          </p>
          <div className="row">
            <button type="button" className="ghost" onClick={() => setModal("signal")}>
              Signal Intelligence
            </button>
          </div>
        </Card>
      </div>

      <p className="hint" style={{ margin: "1rem 0 1.75rem" }}>
        These settings apply to the current workspace:{" "}
        <strong>{workspace.name || "SignalOS"}</strong>.
      </p>

      {/* ------------------------------------------------- editable form -- */}
      {!authed ? (
        <div className="empty">
          <strong>Sign in required</strong>
          Log in with the admin password above to load and edit settings.
        </div>
      ) : (
        <AsyncState loading={settings.loading} error={settings.error}>
          {config && (
            <>
              <div className="accordions">
                {accordion("company", "Company", null, (
                  <div className="two-col">
                    <TextField
                      id="c-name" label="Company name" value={company.name}
                      placeholder="SignalOS"
                      onChange={(v) => patch(["company", "name"], v)}
                    />
                    <TextField
                      id="c-one" label="One-liner" value={company.one_liner}
                      onChange={(v) => patch(["company", "one_liner"], v)}
                    />
                    <ListField
                      id="c-vp" label="Value props (one per line)" value={company.value_props}
                      onChange={(v) => patch(["company", "value_props"], v)}
                    />
                    <ListField
                      id="c-diff" label="Differentiators (one per line)" value={company.differentiators}
                      onChange={(v) => patch(["company", "differentiators"], v)}
                    />
                  </div>
                ))}

                {accordion("icp", "ICP definition", null, (
                  <div className="two-col">
                    <ListField id="i-ind" label="Target industries" value={icp.target_industries}
                      onChange={(v) => patch(["icp", "target_industries"], v)} />
                    <ListField id="i-size" label="Target company sizes" value={icp.target_company_sizes}
                      onChange={(v) => patch(["icp", "target_company_sizes"], v)} />
                    <ListField id="i-stage" label="Target company stages" value={icp.target_company_stages}
                      onChange={(v) => patch(["icp", "target_company_stages"], v)} />
                    <ListField id="i-tech" label="Target tech-stack signals" value={icp.target_tech_stack_signals}
                      onChange={(v) => patch(["icp", "target_tech_stack_signals"], v)} />
                    <ListField id="i-geo" label="Target geographies" value={icp.target_geographies}
                      onChange={(v) => patch(["icp", "target_geographies"], v)} />
                  </div>
                ))}

                {accordion("persona", "Buyer persona", null, (
                  <div className="two-col">
                    <ListField id="p-title" label="Target titles" value={persona.target_titles}
                      onChange={(v) => patch(["persona", "target_titles"], v)} />
                    <ListField id="p-sen" label="Seniority levels" value={persona.seniority_levels}
                      onChange={(v) => patch(["persona", "seniority_levels"], v)} />
                    <ListField id="p-dep" label="Departments" value={persona.departments}
                      onChange={(v) => patch(["persona", "departments"], v)} />
                    <ListField id="p-pain" label="Top pain points" value={persona.top_pain_points}
                      onChange={(v) => patch(["persona", "top_pain_points"], v)} />
                    <ListField id="p-obj" label="Common objections" value={persona.common_objections}
                      onChange={(v) => patch(["persona", "common_objections"], v)} />
                  </div>
                ))}

                {accordion("signals", "Intent signals", null, (
                  <div className="two-col">
                    <ListField
                      id="s-pos" label="Positive signals — boost score / cite if present"
                      value={signals.positive_signals}
                      onChange={(v) => patch(["signals", "positive_signals"], v)} />
                    <ListField
                      id="s-neg" label="Disqualifiers — mark as poor fit, do not target"
                      value={signals.disqualifiers}
                      onChange={(v) => patch(["signals", "disqualifiers"], v)} />
                  </div>
                ))}

                {accordion("news", "News search terms", null, (
                  <ListField
                    id="n-terms" label="Search terms, one per line"
                    value={config.news_search_terms}
                    onChange={(v) => patch(["news_search_terms"], v)} />
                ))}
              </div>

              <div className="save-bar">
                <button type="button" onClick={save} disabled={saving || !dirty}>
                  {saving ? "Saving…" : dirty ? "Save settings" : "No changes"}
                </button>
                {dirty && <span className="muted">Unsaved changes.</span>}
                {saved && <span className={saved.ok ? "ok-text" : "err"}>{saved.message}</span>}
              </div>
            </>
          )}

          {/* --------------------------------------------- integrations -- */}
          <Section title="Instantly integration">
            <Card hint="The API key is shared infrastructure and is never written from this console — only the campaign id is per workspace.">
              <KV rows={[
                ["API key", instantly.api_key_found
                  ? <span className="pill-sm ok">configured ({instantly.api_key_masked})</span>
                  : <span className="pill-sm err">not set</span>],
                ["Key source", instantly.api_key_source],
                ["Campaign source", instantly.campaign_id_source],
              ]} />
              <div className="field" style={{ marginTop: ".9rem" }}>
                <label htmlFor="camp">Campaign ID</label>
                <input
                  id="camp"
                  value={campaignId}
                  placeholder="workspace campaign id"
                  onChange={(e) => setCampaignId(e.target.value)}
                />
              </div>
              <div className="row">
                <button type="button" onClick={saveInstantly}>Save Instantly settings</button>
                {instantlySaved && (
                  <span className={instantlySaved.ok ? "ok-text" : "err"}>
                    {instantlySaved.message}
                  </span>
                )}
              </div>
            </Card>
          </Section>

          <Section title="Email deliverability">
            <Card hint="Verification runs before every send. Keys live in the environment and are never returned to the browser.">
              <KV rows={[
                ["Verifier", deliverability.verifier],
                ["Verifier key", deliverability.verifier_key_configured
                  ? <span className="pill-sm ok">set</span>
                  : <span className="pill-sm err">not set</span>],
              ]} />
              <div className="checks" style={{ marginTop: ".9rem" }}>
                <label className="check">
                  <input type="checkbox" disabled />
                  Allow catch-all emails
                </label>
                <label className="check">
                  <input type="checkbox" disabled />
                  Allow unknown-status emails
                </label>
              </div>
              <p className="state muted">
                Not configurable yet — the send path uses a single strictness
                flag rather than separate toggles, so these are shown disabled
                instead of saving a setting that would not take effect.
              </p>
            </Card>
          </Section>
        </AsyncState>
      )}

      {/* --------------------------------------------------- read-only -- */}
      <Section
        title="Database connection"
        note="What the running app is actually connected to. Host and port only — no credentials."
      >
        <Card>
          <AsyncState loading={status.loading} error={status.error}>
            <KV rows={[
              ["Configured", db.database_configured
                ? <span className="pill-sm ok">yes</span>
                : <span className="pill-sm err">no</span>],
              ["Scheme", db.database_scheme],
              ["Host", <span className="mono">{db.database_host}</span>],
              ["Port", db.database_port],
              ["Uses pooler", db.database_uses_pooler
                ? <span className="pill-sm ok">yes</span>
                : <span className="pill-sm warn">no</span>],
              ["User shape", <span className="mono">{db.database_user_shape}</span>],
            ]} />
            {db.database_warning && (
              <p className="state err">{db.database_warning}</p>
            )}
          </AsyncState>
        </Card>
      </Section>

      <Section title="Configuration" note="Presence only — no value is ever sent to the browser.">
        <Card>
          <AsyncState loading={status.loading} error={status.error}>
            <div className="table-scroll">
              <table className="table">
                <thead><tr><th>Variable</th><th>Status</th><th>Used for</th></tr></thead>
                <tbody>
                  {Object.entries(env).map(([name, present]) => {
                    const required = REQUIRED_ENV.includes(name);
                    return (
                      <tr key={name}>
                        <td className="mono">{name}{required && <span className="req"> *</span>}</td>
                        <td>
                          {present
                            ? <span className="pill-sm ok">configured</span>
                            : <span className={`pill-sm ${required ? "err" : ""}`}>
                                {required ? "missing" : "not set"}
                              </span>}
                        </td>
                        <td className="muted">{ENV_PURPOSE[name] || "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="hint">* required — the API cannot serve data without it.</p>
          </AsyncState>
        </Card>
      </Section>

      <div className="grid-2">
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
          <AsyncState loading={status.loading} error={status.error}>
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

      <Section title="Machine endpoints" note="Called by Instantly and schedulers, not from this console.">
        <Card>
          <ul className="plain">
            <li><span className="method">POST</span><code>/api/instantly/reply-webhook</code> — header <code>X-Webhook-Secret</code></li>
            <li><span className="method">POST</span><code>/api/lead-source/run-scheduled</code> — header <code>X-Job-Secret</code></li>
            <li><span className="method">POST</span><code>/api/v1/drain</code> — bearer key or <code>CRON_SECRET</code></li>
            <li><span className="method">GET</span><code>/health</code> — public</li>
          </ul>
        </Card>
      </Section>

      {modal && <SettingsModal id={modal} onClose={() => setModal(null)} />}
    </>
  );
}

const MODALS = {
  suppressions: {
    title: "Account suppressions",
    body: "There is no suppression list in the database yet. Companies are not currently excluded from outbound by an account-level rule — the send path filters per lead on tier, verification and duplicates.",
  },
  calling: {
    title: "Calling paused",
    body: "There is no calling-pause table yet. Daily Calls is derived live from scored A/B leads that have not been emailed, so nothing is excluded by a meeting-set rule.",
  },
  context: {
    title: "Outbound Context Settings",
    body: "Reason-to-contact weighting and freshness windows are not stored yet. What exists today: the buyer-research news window (90 days by default) and the intent signals defined above.",
  },
  signal: {
    title: "Signal Intelligence",
    body: "Signal imports arrive with the lead payload and through the hiring-signal research run. Live signals are on the Signal Feed page; import history is written to lead_source_imports.",
  },
};

function SettingsModal({ id, onClose }) {
  const modal = MODALS[id];
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <header className="drawer-head">
          <h2>{modal.title}</h2>
          <button type="button" className="ghost" onClick={onClose}>Close</button>
        </header>
        <div className="drawer-body">
          <div className="empty" style={{ marginTop: "1.25rem" }}>
            <strong>Not configured yet</strong>
            {modal.body}
          </div>
        </div>
      </aside>
    </div>
  );
}
