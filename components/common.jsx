/** Small shared pieces: panel states, tier badges, key/value rows. */

export function Panel({ title, hint, actions, children, wide = false }) {
  return (
    <section className={wide ? "card span" : "card"}>
      <div className="card-head">
        <div>
          <h2>{title}</h2>
          {hint && <p className="hint">{hint}</p>}
        </div>
        {actions && <div className="card-actions">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

/** One place to render loading / error / empty so no panel ever renders blank. */
export function AsyncState({ loading, error, empty, emptyText = "Nothing yet.", children }) {
  if (loading) return <p className="state">Loading…</p>;
  if (error) return <p className="state err">{error}</p>;
  if (empty) return <p className="state muted">{emptyText}</p>;
  return children;
}

export function Tier({ value }) {
  if (!value) return <span className="muted">—</span>;
  return <span className={`tier tier-${String(value).toLowerCase()}`}>{value}</span>;
}

export function Bool({ value, yes = "Yes", no = "No" }) {
  return (
    <span className={value ? "ok-text" : "muted"}>{value ? yes : no}</span>
  );
}

export function KV({ rows }) {
  return (
    <dl className="kv">
      {rows.map(([label, value]) => (
        <div key={label} className="kv-row">
          <dt>{label}</dt>
          <dd>{value === null || value === undefined || value === "" ? "—" : value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Stat({ label, value, tone }) {
  return (
    <div className="stat">
      <span className="stat-value" data-tone={tone}>{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  );
}

export function RunStatus({ status }) {
  const tone =
    status === "completed" ? "ok" : status === "failed" ? "err" : status === "running" ? "warn" : "";
  return <span className={`pill-sm ${tone}`}>{status || "—"}</span>;
}
