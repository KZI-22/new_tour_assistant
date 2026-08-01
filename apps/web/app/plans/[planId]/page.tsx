import { AuthGate } from "@/components/auth-gate";
import { TravelPlanView } from "@/components/travel-plan-view";

export default async function TravelPlanPage({
  params,
  searchParams,
}: {
  params: Promise<{ planId: string }>;
  searchParams: Promise<{ version?: string }>;
}) {
  const { planId } = await params;
  const { version: rawVersion } = await searchParams;
  const parsedVersion = rawVersion ? Number.parseInt(rawVersion, 10) : undefined;
  const version = parsedVersion && parsedVersion > 0 ? parsedVersion : undefined;

  return (
    <AuthGate>
      <TravelPlanView key={`${planId}:${version ?? "current"}`} planId={planId} version={version} />
    </AuthGate>
  );
}
