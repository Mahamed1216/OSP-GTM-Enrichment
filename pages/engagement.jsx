import Shell, { useConsole } from "../components/Shell";
import Engagement from "../components/pages/Engagement";

function Body() {
  const { apiKey, openLead } = useConsole();
  return <Engagement apiKey={apiKey} onOpenLead={openLead} />;
}

export default function EngagementRoute() {
  return (
    <Shell title="Engagement">
      <Body />
    </Shell>
  );
}
