import { useLocation, useNavigate } from "react-router-dom";
import { useProjects } from "@/hooks/queries";

/**
 * Flat project status chip for the top bar, styled to match the Claude model
 * status chip (`ClaudeStatsButton`): a live connection dot, the project glyph,
 * and the active project name. Unlike the design mock it drops the "Connected"
 * sub-label — the animated dot carries connection status. Clicking navigates to
 * the Projects screen (it is a status button, not a dropdown).
 */
export function ProjectStatusButton() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { data: projects } = useProjects();

  // Active project: from the URL on a project route, else the first project.
  //
  // The URL segment is the project's GUID (#587), so it is looked up rather than
  // printed — printing it put a raw UUID in the top bar. Falling back to the
  // segment keeps an older name-based link readable while the list loads.
  const projMatch = pathname.match(/^\/projects\/([^/]+)/);
  const routeIdentifier = projMatch ? decodeURIComponent(projMatch[1]) : "";
  const routeProject = routeIdentifier
    ? (projects?.find((p) => p.guid === routeIdentifier) ??
      projects?.find((p) => p.name === routeIdentifier))
    : undefined;
  const activeProject = routeIdentifier
    ? (routeProject?.name ?? routeIdentifier)
    : (projects?.[0]?.name ?? "");
  const connected = Boolean(activeProject);
  const label = activeProject || "No project";

  return (
    <button
      onClick={() => navigate("/projects")}
      className="flex h-[38px] items-center gap-2 rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 text-[12.5px] font-semibold text-ink-soft hover:bg-white/[0.09]"
    >
      <span className="relative flex h-1.5 w-1.5">
        {connected && (
          <span
            className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-70"
            style={{ background: "#34d399" }}
          />
        )}
        <span
          className="relative inline-flex h-1.5 w-1.5 rounded-full"
          style={{ background: connected ? "#34d399" : "#7a7a8c" }}
        />
      </span>
      <span
        className="h-[15px] w-[15px] shrink-0 rounded-[5px]"
        style={{ background: "linear-gradient(135deg,#22d3ee,#6366f1)" }}
      />
      <span className={`whitespace-nowrap ${connected ? "text-ink-soft" : "text-ink-dim"}`}>{label}</span>
    </button>
  );
}
