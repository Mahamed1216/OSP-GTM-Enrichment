import Link from "next/link";
import { useRouter } from "next/router";

/** Fixed left sidebar: SignalOS brand, vertical nav, active highlight. */

export const NAV = [
  ["/dashboard", "Dashboard", "▦"],
  ["/signal-feed", "Signal Feed", "◉"],
  ["/client-expansion", "Client Expansion", "↗"],
  ["/leads", "Leads", "☰"],
  ["/run-pipeline", "Run Pipeline", "▶"],
  ["/apollo-autopilot", "Apollo Autopilot", "◎"],
  ["/settings", "Settings", "⚙"],
  ["/engagement", "Engagement", "✉"],
  ["/prompts", "Prompts", "✎"],
  ["/bdr-research", "BDR Research", "⌕"],
];

export default function Sidebar({ apiKey, onForgetKey }) {
  const router = useRouter();
  // "/" renders the dashboard, so it highlights the same item as /dashboard.
  const path = router.pathname === "/" ? "/dashboard" : router.pathname;

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">
          Signal<span className="os">OS</span>
        </div>
        <div className="brand-sub">Sales Enablement</div>
      </div>

      <nav className="nav">
        {NAV.map(([href, label, icon]) => {
          const active = path === href;
          return (
            <Link
              key={href}
              href={href}
              className={active ? "nav-item active" : "nav-item"}
              aria-current={active ? "page" : undefined}
            >
              <span className="nav-icon" aria-hidden="true">{icon}</span>
              {label}
            </Link>
          );
        })}
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
