import Shell, { useConsole } from "../components/Shell";
import BdrResearch from "../components/pages/BdrResearch";

function Body() {
  const { authed, openLead } = useConsole();
  return <BdrResearch authed={authed} onOpenLead={openLead} />;
}

export default function BdrResearchRoute() {
  return (
    <Shell title="BDR Research">
      <Body />
    </Shell>
  );
}
