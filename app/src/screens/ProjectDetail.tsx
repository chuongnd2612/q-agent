import { useEffect } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Navigate, Outlet, useLocation, useNavigate, useOutletContext, useParams, useSearchParams } from "react-router-dom";
import { useProjectOverviewData } from "./projectDetail/useProjectOverviewData";
import { ProjectHeader } from "./projectDetail/ProjectHeader";
import { ProjectTabsBar } from "./projectDetail/ProjectTabsBar";
import { Overview } from "./projectDetail/Overview";
import { KnowledgeTab } from "./projectDetail/KnowledgeTab";
import { ConnectionTab } from "./projectDetail/connection/ConnectionTab";
import {
  DEFAULT_PROJECT_TAB,
  PROJECT_TABS,
  projectTabPath,
  tabFromLegacyQuery,
  type ProjectTab,
} from "./projectDetail/projectTabs";

// Re-exported for the admin shared-workspace settings page
// (`screens/settings/SharedProjectSettings.tsx`), which reuses the scope-agnostic
// settings form + manual-login view against the shared endpoints.
export { ProjectSettingsForm } from "./projectDetail/ProjectSettingsForm";
export { ManualLoginStatusView } from "./projectDetail/ManualLogin";

/** What the project layout hands to whichever tab is rendered in its outlet.
 *
 * `projectKey` is the identifier the API accepts — the GUID from the URL, or a
 * name if someone followed a pre-#587 bookmark (the layout rewrites the URL, but
 * the first render still has to work with what it was given). */
export interface ProjectRouteContext {
  projectKey: string;
  projectGuid: string | null;
  hubProjectId: string | null;
  providerKind: ReturnType<typeof useProjectOverviewData>["providerKind"];
  repos: ReturnType<typeof useProjectOverviewData>["repoList"];
  meta: ReturnType<typeof useProjectOverviewData>["meta"];
  confidence: number;
  goTab: (tab: ProjectTab) => void;
}

export const useProjectRoute = () => useOutletContext<ProjectRouteContext>();

/** The project context when the caller is rendered inside a project route, else
 * null. `TicketDetail` needs this: it is reachable both nested under a project
 * and through the surviving flat `/tickets/:externalId` deep link. */
export function useOptionalProjectRoute(): ProjectRouteContext | null {
  return (useOutletContext<ProjectRouteContext | null>() ?? null) as ProjectRouteContext | null;
}

/** The active tab, read from the LAST path segment.
 *
 * Deliberately not a `?tab=` param any more (ADR 0015 slice 2): the tab is
 * navigation, so it belongs in the path. Nested deeper routes — a ticket detail
 * under `tickets/:externalId` — still light up their parent tab, which is why
 * this scans the segments for a known tab rather than reading only the last one.
 */
function activeTabFromPath(pathname: string): ProjectTab {
  const segments = pathname.split("/").filter(Boolean);
  const found = segments.find((segment) =>
    PROJECT_TABS.some((tab) => tab.id === segment),
  );
  return (found as ProjectTab | undefined) ?? DEFAULT_PROJECT_TAB;
}

/**
 * Project layout — the container everything ticket- and run-shaped now lives
 * inside (ADR 0015). Owns the header and the six-tab bar; the tab itself renders
 * through `<Outlet/>`, so each tab is a real route with its own URL.
 */
export function ProjectDetail() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { projectGuid } = useParams();
  // The route param is the project's GUID (#587). It may still be a *name* when
  // someone follows a pre-#587 bookmark, so it is passed through as an opaque
  // identifier — the API resolves either — and the URL is rewritten below.
  const key = decodeURIComponent(projectGuid ?? "");
  const [searchParams] = useSearchParams();
  const activeTab = activeTabFromPath(pathname);

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
      navigate(pathname.replace(encodeURIComponent(key), encodeURIComponent(canonicalGuid)), {
        replace: true,
      });
    }
  }, [canonicalGuid, key, navigate, pathname]);

  // Pre-#728 bookmarks carry `?tab=`. Translate it to the path once and drop the
  // param, rather than supporting two ways to say the same thing.
  const legacyTab = searchParams.get("tab");
  useEffect(() => {
    if (legacyTab) {
      navigate(projectTabPath(key, tabFromLegacyQuery(legacyTab)), { replace: true });
    }
  }, [legacyTab, key, navigate]);

  const goTab = (tab: ProjectTab) => navigate(projectTabPath(key, tab));

  const context: ProjectRouteContext = {
    projectKey: key,
    projectGuid: canonicalGuid ?? null,
    hubProjectId: project?.hubProjectId ?? null,
    providerKind,
    repos: repoList,
    meta,
    confidence,
    goTab,
  };

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

      <ProjectTabsBar active={activeTab} onSelect={goTab} />

      <AnimatePresence mode="wait">
        <motion.div
          key={activeTab}
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.2 }}
        >
          <Outlet context={context} />
        </motion.div>
      </AnimatePresence>
    </div>
  );
}

/** `/projects/:projectGuid` with no tab — the overview, canonically addressed. */
export function ProjectTabIndex() {
  return <Navigate to={DEFAULT_PROJECT_TAB} replace />;
}

export function ProjectOverviewTab() {
  const { meta, confidence, goTab } = useProjectRoute();
  return <Overview meta={meta} confidence={confidence} onView={() => goTab("knowledge")} />;
}

export function ProjectKnowledgeTab() {
  const { projectKey, providerKind, repos, goTab } = useProjectRoute();
  return (
    <KnowledgeTab
      projectKey={projectKey}
      providerKind={providerKind}
      repos={repos}
      onManageRepos={() => goTab("connection")}
    />
  );
}

/** The Connection tab — the project's three connection roles (ADR 0015 §3, #732):
 * TICKET SOURCE, CODE & KNOWLEDGE and TEST CASE TARGET. The only place a
 * project's provider is chosen; the ticket flow reads the binding made here and
 * offers no switcher of its own. */
export function ProjectConnectionTab() {
  const { projectKey, hubProjectId } = useProjectRoute();
  return <ConnectionTab projectKey={projectKey} hubProjectId={hubProjectId} />;
}
