import { FolderGit2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/misc";
import { useProjectRoute } from "@/screens/ProjectDetail";

/**
 * The project's Automation tab (#765) — where the specs and the automation repo
 * a project has accumulated are browsed, outside any run.
 *
 * This is the shared-core slice's placeholder (#767): the tab exists, routes and
 * builds **before** the project-keyed read endpoints do (#768), so every later
 * slice lands against a tab that is already wired. It therefore renders the
 * "nothing here yet" state unconditionally for now.
 *
 * Deliberately offers no Bootstrap/Adopt button: creating a repo by hand is ADR
 * 0014 slice 2 and out of scope here, and a button that does nothing is worse
 * than no button. The one exit is to the project's Runs tab, which is where an
 * automation repo actually comes from.
 *
 * Lives in `screens/projectAutomation/` rather than `screens/automation/` — that
 * directory is the RUN overlay's, and keeping the two apart is what makes the
 * remaining slices file-disjoint.
 */
export function ProjectAutomationTab() {
  const { t } = useTranslation("projects");
  const { goTab } = useProjectRoute();
  return (
    <EmptyState
      icon={<FolderGit2 size={28} className="text-muted" />}
      title={t("automation.emptyTitle")}
      body={t("automation.emptyBody")}
      action={
        <Button variant="primary" onClick={() => goTab("runs")}>
          {t("automation.viewRuns")}
        </Button>
      }
    />
  );
}
