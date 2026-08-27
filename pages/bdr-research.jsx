import Shell, { useConsole } from "../components/Shell";
import BdrResearch from "../components/pages/BdrResearch";

function Body() {
  const { apiKey, openLead } = useConsole();
  return <BdrResearch apiKey={apiKey} onOpenLead={openLead} />;
}

export default function BdrResearchRoute() {
  return (
    <Shell title="BDR Research">
      <Body />
    </Shell>
  );
}
