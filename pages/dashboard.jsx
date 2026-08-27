import Shell, { useConsole } from "../components/Shell";
import Dashboard from "../components/pages/Dashboard";

function Body() {
  const { authed, openLead, health } = useConsole();
  return <Dashboard authed={authed} onOpenLead={openLead} health={health} />;
}

export default function DashboardRoute() {
  return (
    <Shell title="Dashboard">
      <Body />
    </Shell>
  );
}
