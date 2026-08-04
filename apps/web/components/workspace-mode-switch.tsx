import { Bot, MapPinned } from "lucide-react";
import Link from "next/link";

type WorkspaceMode = "workspace" | "agent";

export function WorkspaceModeSwitch({ active }: { active: WorkspaceMode }) {
  const linkClass = (mode: WorkspaceMode) =>
    `flex items-center justify-center gap-1.5 rounded-xl px-2.5 py-2 text-xs font-medium transition-colors sm:px-3 ${
      active === mode
        ? "bg-[#e8f5ef] text-[#0f766e]"
        : "text-[#697586] hover:bg-black/[0.04] hover:text-[#334155]"
    }`;

  return (
    <nav aria-label="工作模式" className="flex items-center rounded-2xl bg-[#f5f7f5] p-1">
      <Link aria-current={active === "workspace" ? "page" : undefined} className={linkClass("workspace")} href="/">
        <MapPinned size={15} />
        <span className="hidden sm:inline">旅游工作台</span>
      </Link>
      <Link aria-current={active === "agent" ? "page" : undefined} className={linkClass("agent")} href="/agent">
        <Bot size={15} />
        <span className="hidden sm:inline">Agent Loop</span>
      </Link>
    </nav>
  );
}
