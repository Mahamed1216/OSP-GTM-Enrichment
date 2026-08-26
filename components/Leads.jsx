import { useEffect, useMemo, useState } from "react";

import { useApi } from "../lib/api";
import { AsyncState, Bool, Panel, Tier } from "./common";

const PAGE = 25;

export default function Leads({ apiKey, onOpenLead }) {
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const [tier, setTier] = useState("");
  const [enrichedOnly, setEnrichedOnly] = useState(false);
  const [sentFilter, setSentFilter] = useState("");
  const [offset, setOffset] = useState(0);

  // Debounce typing so each keystroke is not a request.
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search]);

  // Any filter change returns to the first page.
  useEffect(() => {
    setOffset(0);
  }, [debounced, tier, enrichedOnly, sentFilter]);

  const path = useMemo(() => {
    const params = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
    if (debounced) params.set("search", debounced);
    if (tier) params.set("tier", tier);
    if (enrichedOnly) params.set("enriched_only", "true");
    if (sentFilter === "sent") params.set("sent_only", "true");
    if (sentFilter === "not_sent") params.set("not_sent_only", "true");
    return `/api/v1/leads?${params.toString()}`;
  }, [debounced, tier, enrichedOnly, sentFilter, offset]);

  const { data, loading, error, reload } = useApi(path, apiKey, { skip: !apiKey });
  const rows = data?.leads || [];
  const total = data?.total ?? 0;

  return (
    <Panel
      title="Leads"
      hint={loading ? "Loading…" : `${total} lead${total === 1 ? "" : "s"} matching the current filters.`}
      actions={
        <button type="button" className="ghost" onClick={reload}>
          Refresh
        </button>
      }
      wide
    >
      <div className="filters">
        <div className="grow">
          <label htmlFor="q">Search</label>
          <input
            id="q"
            value={search}
            placeholder="name, company or email"
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="tier">Tier</label>
          <select id="tier" value={tier} onChange={(e) => setTier(e.target.value)}>
            <option value="">All</option>
            <option value="A">A</option>
            <option value="B">B</option>
            <option value="C">C</option>
            <option value="D">D</option>
          </select>
        </div>
        <div>
          <label htmlFor="sent">Delivery</label>
          <select id="sent" value={sentFilter} onChange={(e) => setSentFilter(e.target.value)}>
            <option value="">All</option>
            <option value="sent">Sent</option>
            <option value="not_sent">Not sent</option>
          </select>
        </div>
        <div className="checkbox">
          <label htmlFor="enr">
            <input
              id="enr"
              type="checkbox"
              checked={enrichedOnly}
              onChange={(e) => setEnrichedOnly(e.target.checked)}
            />
            Enriched only
          </label>
        </div>
      </div>

      <AsyncState
        loading={loading}
        error={error}
        empty={!loading && rows.length === 0}
        emptyText="No leads match these filters."
      >
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Name</th><th>Title</th><th>Company</th><th>Email</th>
                <th>Tier</th><th>Score</th><th>Enriched</th>
                <th>Hiring</th><th>Src signal</th><th>Sent</th><th>Replied</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className="clickable" onClick={() => onOpenLead(row.id)}>
                  <td>{row.Name || <span className="muted">—</span>}</td>
                  <td className="muted">{row.Title || "—"}</td>
                  <td>{row.Company || <span className="muted">—</span>}</td>
                  <td className="muted mono">{row.Email || "—"}</td>
                  <td><Tier value={row.Tier} /></td>
                  <td>{row.Score ?? <span className="muted">—</span>}</td>
                  <td><Bool value={row.Enriched} /></td>
                  <td className="muted">{row.Hiring || "—"}</td>
                  <td className="muted">{row["Src signal"] || "—"}</td>
                  <td><Bool value={row.Sent} /></td>
                  <td><Bool value={row.Replied} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </AsyncState>

      <div className="pager">
        <button
          type="button"
          className="ghost"
          disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - PAGE))}
        >
          ← Previous
        </button>
        <span className="muted">
          {total === 0 ? "0" : `${offset + 1}–${Math.min(offset + PAGE, total)}`} of {total}
        </span>
        <button
          type="button"
          className="ghost"
          disabled={offset + PAGE >= total}
          onClick={() => setOffset(offset + PAGE)}
        >
          Next →
        </button>
      </div>
    </Panel>
  );
}
