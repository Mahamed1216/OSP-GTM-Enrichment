import Shell, { useConsole } from "../components/Shell";
import Prompts from "../components/pages/Prompts";

function Body() {
  const { apiKey } = useConsole();
  return <Prompts apiKey={apiKey} />;
}

export default function PromptsRoute() {
  return (
    <Shell title="Prompts">
      <Body />
    </Shell>
  );
}
