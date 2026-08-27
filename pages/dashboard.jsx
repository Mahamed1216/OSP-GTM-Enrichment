import Shell, { useConsole } from "../components/Shell";
import Dashboard from "../components/pages/Dashboard";

function Body() {
  const { apiKey, openLead, health } = useConsole();
  return <Dashboard apiKey={apiKey} onOpenLead={openLead} health={health} />;
}

export default function DashboardRoute() {
  return (
    <Shell title="Dashboard">
      <Body />
    </Shell>
  );
}
