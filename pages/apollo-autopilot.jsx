import Shell, { useConsole } from "../components/Shell";
import ApolloAutopilot from "../components/pages/ApolloAutopilot";

function Body() {
  return <ApolloAutopilot />;
}

export default function ApolloAutopilotRoute() {
  return (
    <Shell title="Apollo Autopilot">
      <Body />
    </Shell>
  );
}
