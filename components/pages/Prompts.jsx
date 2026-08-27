import { useCallback, useEffect, useState } from "react";

import { callApi, formatDate, useApi } from "../../lib/api";
import { AsyncState, Card, PageHead, RunStatus, Section } from "../common";

// The loader reports where the effective prompt came from.
const SOURCE_LABELS = {
  database: "Database",
  local_json: "Local JSON file (deprecated fallback)",
  code_default: "Built-in default",
  default: "Built-in default",
};

const CHANNELS = [
  ["email", "Email"],
  ["linkedin_msg", "LinkedIn DM"],
  ["call_script", "Call script"],
];

/**
 * Section editor for the generation prompt.
 *
 * Sections come from the prompt itself (headers in the live text), not a fixed
 * list — a hardcoded list would drop a section the prompt has or invent empty
 * ones it doesn't. Saving recombines the sections and writes through the same
 * loader the generator reads, so the fingerprint and self-improvement loop stay
 * consistent.
 */
export default function Prompts({ authed }) {
  const [channel, setChannel] = useState("email");
  const [sections, setSections] = useState([]);
  const [open, setOpen] = useState(() => new Set());
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState(null);
  const [preview, setPreview] = useState(null);
  const [adding, setAdding] = useState("");

  const { data, loading, error, reload } = useApi(
    `/api/v1/prompts/editor?channel=${channel}`,
    { skip: !authed },
  );

  // Load the fetched sections into editable state once per fetch.
  useEffect(() => {
    if (!data) return;
    setSections(data.sections || []);
    setDirty(false);
    setResult(null);
  }, [data]);

  const meta = data?.metadata || {};
  const available = data?.available_sections || [];

  const toggle = useCallback((index) => {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }, []);

  function editBody(index, body) {
    setSections((prev) => prev.map((s, i) => (i === index ? { ...s, body } : s)));
    setDirty(true);
  }

  function addSection(title) {
    if (!title) return;
    setSections((prev) => [...prev, { title, body: "" }]);
    setOpen((prev) => new Set(prev).add(sections.length));
    setAdding("");
    setDirty(true);
  }

  function removeSection(index) {
    setSections((prev) => prev.filter((_, i) => i !== index));
    setDirty(true);
  }

  async function save() {
    setSaving(true);
    setResult(null);
    const response = await callApi("/api/v1/prompts/editor", {
      method: "POST",
      body: { channel, sections },
    });
    setSaving(false);
    if (response.ok) {
      setDirty(false);
      setResult({ ok: true, message: `Saved — ${response.data.length} characters.` });
      reload();
    } else {
      setResult({ ok: false, message: response.error });
    }
  }

  const compiled =
    sections
      .map((s) => (s.title ? `# ${s.title}\n${s.body}` : s.body))
      .filter((part) => part.trim())
      .join("\n\n") + "\n";

  return (
    <>
      <PageHead
        title="Prompts."
        note="Edit the brain. Each section is editable; save recombines them into the full SignalOS prompt."
      />

      <div className="filters">
        <div className="subtabs">
          {CHANNELS.map(([id, label]) => (
            <button
              key={id}
              type="button"
              className={channel === id ? "subtab active" : "subtab"}
              onClick={() => setChannel(id)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {!authed ? (
        <div className="empty">
          <strong>Sign in required</strong>
          Log in with the admin password above to load and edit prompts.
        </div>
      ) : (
        <AsyncState loading={loading} error={error}>
          <div className="meta-row">
            <span className="muted">
              Loaded from: <strong>{SOURCE_LABELS[data?.source] || data?.source || "unknown"}</strong>
              {meta.updated_at && (
                <> · Last saved {formatDate(meta.updated_at)} by {meta.updated_by || "unknown"}</>
              )}
              {meta.prompt_fingerprint && (
                <> · fingerprint <code>{meta.prompt_fingerprint}</code></>
              )}
            </span>
            <button type="button" className="ghost" onClick={() => setPreview(compiled)}>
              Preview compiled prompt
            </button>
          </div>

          {sections.length === 0 ? (
            <div className="empty">
              <strong>No prompt sections</strong>
              This channel has no prompt text yet. Add a section below to start one.
            </div>
          ) : (
            <div className="accordions">
              {sections.map((section, index) => (
                <div key={`${section.title}-${index}`} className="accordion">
                  <button
                    type="button"
                    className="accordion-head"
                    aria-expanded={open.has(index)}
                    onClick={() => toggle(index)}
                  >
                    <span className="accordion-caret">{open.has(index) ? "▾" : "▸"}</span>
                    <span className="accordion-title">{section.title || "(preamble)"}</span>
                    <span className="muted accordion-size">{section.body.length} chars</span>
                  </button>
                  {open.has(index) && (
                    <div className="accordion-body">
                      <textarea
                        value={section.body}
                        spellCheck={false}
                        onChange={(e) => editBody(index, e.target.value)}
                      />
                      <div className="row">
                        <button
                          type="button"
                          className="ghost danger"
                          onClick={() => removeSection(index)}
                        >
                          Remove section
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          <Card title="Add a section" hint="Appended to the end of the prompt; reorder by editing the text.">
            {available.length === 0 ? (
              <p className="state muted">
                Every suggested section is already present in this prompt.
              </p>
            ) : (
              <div className="filters" style={{ marginBottom: 0 }}>
                <div className="grow">
                  <label htmlFor="add-section">Section</label>
                  <select
                    id="add-section"
                    value={adding}
                    onChange={(e) => setAdding(e.target.value)}
                  >
                    <option value="">Choose a section…</option>
                    {available.map((title) => (
                      <option key={title} value={title}>{title}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label>&nbsp;</label>
                  <button type="button" onClick={() => addSection(adding)} disabled={!adding}>
                    Add
                  </button>
                </div>
              </div>
            )}
          </Card>

          <div className="save-bar">
            <button type="button" onClick={save} disabled={saving || !dirty}>
              {saving ? "Saving…" : dirty ? "Save prompt" : "No changes"}
            </button>
            {dirty && <span className="muted">Unsaved changes.</span>}
            {result && (
              <span className={result.ok ? "ok-text" : "err"}>{result.message}</span>
            )}
          </div>
        </AsyncState>
      )}

      {authed && <SelfImprovement authed={authed} />}

      {preview !== null && (
        <div className="drawer-backdrop" onClick={() => setPreview(null)}>
          <aside className="drawer" onClick={(e) => e.stopPropagation()}>
            <header className="drawer-head">
              <div>
                <h2>Compiled prompt</h2>
                <p className="hint">
                  {preview.length} characters — exactly what the generator receives.
                </p>
              </div>
              <button type="button" className="ghost" onClick={() => setPreview(null)}>
                Close
              </button>
            </header>
            <pre className="content-body" style={{ maxHeight: "none" }}>{preview}</pre>
          </aside>
        </div>
      )}
    </>
  );
}


/** Recommendations and winning examples — read-only, from /api/v1/prompts. */
function SelfImprovement({ authed }) {
  const [open, setOpen] = useState(false);
  const { data, loading, error } = useApi("/api/v1/prompts", { skip: !authed || !open });

  const recommendations = data?.recommendations || [];
  const winners = data?.winners || [];

  return (
    <Section title="Self-improvement">
      <Card
        title="Recommendations and winning examples"
        hint="Produced by the feedback loop. Every prompt change stays gated on human approval."
        actions={
          <button type="button" className="ghost" onClick={() => setOpen((v) => !v)}>
            {open ? "Hide" : "Show"}
          </button>
        }
      >
        {!open ? (
          <p className="state muted">Collapsed — expand to load.</p>
        ) : (
          <AsyncState loading={loading} error={error}>
            <h4>Recommendations</h4>
            {recommendations.length === 0 ? (
              <p className="state muted">
                None yet — the loop needs engagement data before it proposes a change.
              </p>
            ) : (
              <ul className="plain">
                {recommendations.map((rec) => (
                  <li key={rec.id}>
                    <RunStatus status={rec.status || rec.loop_status} />{" "}
                    <strong>{rec.bottleneck || rec.channel}</strong>
                    {rec.recommended_change && (
                      <div className="muted">{rec.recommended_change}</div>
                    )}
                  </li>
                ))}
              </ul>
            )}

            <h4>Winning examples</h4>
            {winners.length === 0 ? (
              <p className="state muted">
                None yet — promote a high-performing email to seed the library.
              </p>
            ) : (
              <ul className="plain">
                {winners.map((winner) => (
                  <li key={winner.id}>
                    <strong>{winner.subject || "(no subject)"}</strong>
                    {winner.reply_rate != null && (
                      <span className="muted"> · reply rate {winner.reply_rate}</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </AsyncState>
        )}
      </Card>
    </Section>
  );
}
