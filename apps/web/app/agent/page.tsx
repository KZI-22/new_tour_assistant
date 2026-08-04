import { AuthGate } from "@/components/auth-gate";
import { ChatShell } from "@/components/chat-shell";

export default function AgentLoopPage() {
  return (
    <AuthGate>
      <ChatShell />
    </AuthGate>
  );
}
