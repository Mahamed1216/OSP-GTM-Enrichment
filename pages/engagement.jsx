import Shell, { useConsole } from "../components/Shell";
import Engagement from "../components/pages/Engagement";

function Body() {
  const { authed, openLead } = useConsole();
  return <Engagement authed={authed} onOpenLead={openLead} />;
}

export default function EngagementRoute() {
  return (
    <Shell title="Engagement">
      <Body />
    </Shell>
  );
}
