import Shell, { useConsole } from "../components/Shell";
import RunPipeline from "../components/pages/RunPipeline";

function Body() {
  const { authed } = useConsole();
  return <RunPipeline authed={authed} />;
}

export default function RunPipelineRoute() {
  return (
    <Shell title="Run Pipeline">
      <Body />
    </Shell>
  );
}
