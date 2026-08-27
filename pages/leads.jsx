import Shell, { useConsole } from "../components/Shell";
import Leads from "../components/pages/Leads";

function Body() {
  const { authed, openLead } = useConsole();
  return <Leads authed={authed} onOpenLead={openLead} />;
}

export default function LeadsRoute() {
  return (
    <Shell title="Leads">
      <Body />
    </Shell>
  );
}
