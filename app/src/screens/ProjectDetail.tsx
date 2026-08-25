import { useEffect } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { type ProjectTab } from "@/store/ui";
import { useProjectOverviewData } from "./projectDetail/useProjectOverviewData";
import { ProjectHeader } from "./projectDetail/ProjectHeader";
import { ProjectTabsBar, TABS } from "./projectDetail/ProjectTabsBar";
import { Overview } from "./projectDetail/Overview";
import { KnowledgeTab } from "./projectDetail/KnowledgeTab";
import { ProjectSettingsTab } from "./projectDetail/ProjectSettingsTab";

// Re-exported for the admin shared-workspace settings page
// (`screens/settings/SharedProjectSettings.tsx`), which reuses the scope-agnostic
// settings form + manual-login view against the shared endpoints.
export { ProjectSettingsForm } from "./projectDetail/ProjectSettingsForm";
export { ManualLoginStatusView } from "./projectDetail/ManualLogin";

export function ProjectDetail() {
  const navigate = useNavigate();
  const { projectGuid } = useParams();
  // The route param is the project's GUID (#587). It may still be a *name* when
  // someone follows a pre-#587 bookmark, so it is passed through as an opaque
  // identifier — the API resolves either — and the URL is rewritten below.
  const key = decodeURIComponent(projectGuid ?? "");
  const [searchParams, setSearchParams] = useSearchParams();
  // `tickets`/`runs` are no longer tabs (#693). A pre-#693 bookmark can still carry
  // `?tab=runs`, which would otherwise fall through to the Knowledge tab while the
  // URL claims otherwise — so an unknown tab reads as `overview`.
  const requestedTab = searchParams.get("tab") as ProjectTab | null;
  const projectTab: ProjectTab = TABS.some((tab) => tab.id === requestedTab)
    ? (requestedTab as ProjectTab)
    : "overview";
  const setProjectTab = (t: ProjectTab) => setSearchParams({ tab: t });

  const {
    project,
    meta,
    providerKind,
    repoList,
    confidence,
    statusColor,
    statusBg,
    statusDot,
    statusLabel,
    glyph,
    glyphBg,
    glyphColor,
  } = useProjectOverviewData(key);

  // Canonicalise a name-based deep link to its GUID, in place. The screen works
  // either way (the API resolves both), so this is not a fix for a broken link —
  // it stops a copied URL from spreading the identifier we are retiring.
  const canonicalGuid = project?.guid;
  useEffect(() => {
    if (canonicalGuid && canonicalGuid !== key) {
      navigate(
        { pathname: `/projects/${encodeURIComponent(canonicalGuid)}`, search: searchParams.toString() },
        { replace: true },
      );
    }
  }, [canonicalGuid, key, navigate, searchParams]);

  const onTab = (id: ProjectTab) => setProjectTab(id);

  return (
    <div className="px-1 pb-10 pt-0.5">
      <ProjectHeader
        meta={meta}
        glyph={glyph}
        glyphBg={glyphBg}
        glyphColor={glyphColor}
        statusBg={statusBg}
        statusDot={statusDot}
        statusColor={statusColor}
        statusLabel={statusLabel}
        onBack={() => navigate("/projects")}
      />

      <ProjectTabsBar active={projectTab} onSelect={onTab} />

      <AnimatePresence mode="wait">
        <motion.div
          key={projectTab}
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.2 }}
        >
          {projectTab === "overview" ? (
            <Overview meta={meta} confidence={confidence} onView={() => setProjectTab("knowledge")} />
          ) : projectTab === "settings" ? (
            <ProjectSettingsTab projectKey={key} hubProjectId={project?.hubProjectId ?? null} />
          ) : (
            <KnowledgeTab
              projectKey={key}
              providerKind={providerKind}
              repos={repoList}
              onManageRepos={() => setProjectTab("settings")}
            />
          )}
        </motion.div>
      </AnimatePresence>
    </div>
  );
}
