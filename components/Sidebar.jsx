/** Fixed left sidebar: brand, vertical nav, active highlight. */

export const NAV = [
  ["dashboard", "Dashboard", "▦"],
  ["signals", "Signal Feed", "◉"],
  ["expansion", "Client Expansion", "↗"],
  ["leads", "Leads", "☰"],
  ["pipeline", "Run Pipeline", "▶"],
  ["apollo", "Apollo Autopilot", "◎"],
  ["settings", "Settings", "⚙"],
  ["engagement", "Engagement", "✉"],
  ["prompts", "Prompts", "✎"],
  ["research", "BDR Research", "⌕"],
];

export default function Sidebar({ current, onNavigate, apiKey, onForgetKey }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">
          Cloudwork<span className="bar">|</span><span className="pro">PRO</span>
        </div>
        <div className="brand-sub">Sales Enablement</div>
      </div>

      <nav className="nav">
        {NAV.map(([id, label, icon]) => (
          <button
            key={id}
            type="button"
            className={current === id ? "nav-item active" : "nav-item"}
            aria-current={current === id ? "page" : undefined}
            onClick={() => onNavigate(id)}
          >
            <span className="nav-icon" aria-hidden="true">{icon}</span>
            {label}
          </button>
        ))}
      </nav>

      <div className="sidebar-foot">
        {apiKey ? (
          <button type="button" className="linkish" onClick={onForgetKey}>
            Forget API key
          </button>
        ) : (
          <span>API key not set</span>
        )}
      </div>
    </aside>
  );
}
