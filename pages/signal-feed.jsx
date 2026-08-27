import Shell, { useConsole } from "../components/Shell";
import SignalFeed from "../components/pages/SignalFeed";

function Body() {
  const { apiKey, openLead } = useConsole();
  return <SignalFeed apiKey={apiKey} onOpenLead={openLead} />;
}

export default function SignalFeedRoute() {
  return (
    <Shell title="Signal Feed">
      <Body />
    </Shell>
  );
}
