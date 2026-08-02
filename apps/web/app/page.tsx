import { AuthGate } from "@/components/auth-gate";
import { TravelWorkspace } from "@/components/travel-workspace";

export default function Home() {
  return (
    <AuthGate>
      <TravelWorkspace />
    </AuthGate>
  );
}
