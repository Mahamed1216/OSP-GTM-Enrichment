import Shell, { useConsole } from "../components/Shell";
import Settings from "../components/pages/Settings";

function Body() {
  const { authed, health } = useConsole();
  return <Settings authed={authed} health={health} />;
}

export default function SettingsRoute() {
  return (
    <Shell title="Settings">
      <Body />
    </Shell>
  );
}
