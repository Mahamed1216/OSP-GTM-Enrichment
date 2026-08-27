import Shell, { useConsole } from "../components/Shell";
import ClientExpansion from "../components/pages/ClientExpansion";

function Body() {
  const { apiKey, openLead } = useConsole();
  return <ClientExpansion apiKey={apiKey} onOpenLead={openLead} />;
}

export default function ClientExpansionRoute() {
  return (
    <Shell title="Client Expansion">
      <Body />
    </Shell>
  );
}
