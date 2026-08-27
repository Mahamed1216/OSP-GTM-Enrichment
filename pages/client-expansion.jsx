import Shell, { useConsole } from "../components/Shell";
import ClientExpansion from "../components/pages/ClientExpansion";

function Body() {
  const { authed, openLead } = useConsole();
  return <ClientExpansion authed={authed} onOpenLead={openLead} />;
}

export default function ClientExpansionRoute() {
  return (
    <Shell title="Client Expansion">
      <Body />
    </Shell>
  );
}
