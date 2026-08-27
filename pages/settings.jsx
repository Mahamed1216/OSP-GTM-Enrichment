import Shell, { useConsole } from "../components/Shell";
import Settings from "../components/pages/Settings";

function Body() {
  const { apiKey, health } = useConsole();
  return <Settings apiKey={apiKey} health={health} />;
}

export default function SettingsRoute() {
  return (
    <Shell title="Settings">
      <Body />
    </Shell>
  );
}
