import { AuthGate } from "@/components/auth-gate";
import { ChatShell } from "@/components/chat-shell";

export default function Home() {
  return (
    <AuthGate>
      <ChatShell />
    </AuthGate>
  );
}
