import Shell, { useConsole } from "../components/Shell";
import Prompts from "../components/pages/Prompts";

function Body() {
  const { authed } = useConsole();
  return <Prompts authed={authed} />;
}

export default function PromptsRoute() {
  return (
    <Shell title="Prompts">
      <Body />
    </Shell>
  );
}
