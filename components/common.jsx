/** Shared pieces: cards, sections, metric tiles, tier chips, async states. */

export function Section({ title, note, actions, children }) {
  return (
    <section className="section">
      <div className="section-head">
        <div>
          <h2>{title}</h2>
          {note && <p>{note}</p>}
        </div>
        {actions}
      </div>
      {children}
    </section>
  );
}

export function Card({ title, hint, actions, children }) {
  return (
    <div className="card">
      {(title || actions) && (
        <div className="card-head">
          <div>
            {title && <h3>{title}</h3>}
            {hint && <p className="hint">{hint}</p>}
          </div>
          {actions}
        </div>
      )}
      {children}
    </div>
  );
}

export function PageHead({ title, note }) {
  return (
    <div className="page-head">
      <h1>{title}</h1>
      {note && <p>{note}</p>}
    </div>
  );
}

/** One metric tile. `accent` outlines the primary figure of a row. */
export function Metric({ label, value, note, accent = false, chip }) {
  return (
    <div className="metric" data-accent={accent ? "true" : undefined}>
      {chip ? (
        <div className="metric-tier">
          {chip}
          <span className="metric-value">{value}</span>
        </div>
      ) : (
        <span className="metric-value">{value}</span>
      )}
      <span className="metric-label">{label}</span>
      {note && <span className="metric-note">{note}</span>}
    </div>
  );
}

/** Tier letter is always shown, so colour is never the only cue. */
export function TierChip({ value }) {
  const key = String(value || "").toLowerCase();
  const known = ["a", "b", "c", "d"].includes(key);
  return (
    <span className={`tier-chip ${known ? `tier-${key}` : "tier-none"}`}>
      {known ? value.toUpperCase() : "–"}
    </span>
  );
}

export function Tier({ value }) {
  if (!value) return <span className="muted">—</span>;
  return <TierChip value={value} />;
}

/** No panel ever renders blank: loading, error and empty all have a shape. */
export function AsyncState({ loading, error, empty, emptyTitle, emptyText, children }) {
  if (loading) return <p className="state muted">Loading…</p>;
  if (error) return <p className="state err">{error}</p>;
  if (empty) {
    return (
      <div className="empty">
        {emptyTitle && <strong>{emptyTitle}</strong>}
        {emptyText || "Nothing here yet."}
      </div>
    );
  }
  return children;
}

export function Bool({ value, yes = "Yes", no = "No" }) {
  return <span className={value ? "ok-text" : "muted"}>{value ? yes : no}</span>;
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

export function RunStatus({ status }) {
  const tone =
    status === "completed" || status === "sent" ? "ok"
      : status === "failed" || status === "error" ? "err"
        : status === "running" || status === "in_progress" ? "warn"
          : "";
  return <span className={`pill-sm ${tone}`}>{status || "—"}</span>;
}

export function Strength({ value }) {
  if (!value) return <span className="muted">—</span>;
  const key = String(value).toLowerCase();
  const tone = key === "high" ? "ok" : key === "medium" ? "warn" : "";
  return <span className={`pill-sm ${tone}`}>{value}</span>;
}
