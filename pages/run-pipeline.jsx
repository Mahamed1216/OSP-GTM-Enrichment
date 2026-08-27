import Shell, { useConsole } from "../components/Shell";
import RunPipeline from "../components/pages/RunPipeline";

function Body() {
  const { apiKey } = useConsole();
  return <RunPipeline apiKey={apiKey} />;
}

export default function RunPipelineRoute() {
  return (
    <Shell title="Run Pipeline">
      <Body />
    </Shell>
  );
}
