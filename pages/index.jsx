import Shell, { useConsole } from "../components/Shell";
import Dashboard from "../components/pages/Dashboard";

/**
 * SignalOS operator console — homepage.
 *
 * "/" renders the dashboard directly rather than redirecting to /dashboard, so
 * the root stays a statically prerendered HTML page. Both routes show the same
 * view and the sidebar highlights Dashboard for either.
 *
 * Statically prerendered: no getServerSideProps, no environment variables, no
 * database. Every panel fetches client-side and renders its own error state.
 */

function Body() {
  const { authed, openLead, health } = useConsole();
  return <Dashboard authed={authed} onOpenLead={openLead} health={health} />;
}

export default function Home() {
  return (
    <Shell>
      <Body />
    </Shell>
  );
}
