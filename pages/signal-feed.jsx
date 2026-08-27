import Shell, { useConsole } from "../components/Shell";
import SignalFeed from "../components/pages/SignalFeed";

function Body() {
  const { authed, openLead } = useConsole();
  return <SignalFeed authed={authed} onOpenLead={openLead} />;
}

export default function SignalFeedRoute() {
  return (
    <Shell title="Signal Feed">
      <Body />
    </Shell>
  );
}
