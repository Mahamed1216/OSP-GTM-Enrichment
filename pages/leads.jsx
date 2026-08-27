import Shell, { useConsole } from "../components/Shell";
import Leads from "../components/pages/Leads";

function Body() {
  const { apiKey, openLead } = useConsole();
  return <Leads apiKey={apiKey} onOpenLead={openLead} />;
}

export default function LeadsRoute() {
  return (
    <Shell title="Leads">
      <Body />
    </Shell>
  );
}
