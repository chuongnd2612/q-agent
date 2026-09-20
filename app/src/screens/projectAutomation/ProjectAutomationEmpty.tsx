import type { CSSProperties } from "react";
import { FileCode2, FolderGit2, MousePointerClick } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/misc";

/**
 * The project Automation tab's "nothing to browse" surfaces (#770).
 *
 * Three states, and the third is deliberately **not** one of them:
 *
 * 1. {@link NoRepoEmpty} — no `AutomationProject` row at all. The whole tab.
 * 2. {@link ScaffoldOnlyEmpty} — the repo exists but has mirrored zero files,
 *    which happens for real: `ensure_project` materializes the skeleton and
 *    commits it *before* anything is generated. Occupies only the file area, so
 *    the repo header, the selector and the ZIP button stay — the scaffold is a
 *    legitimate download and hiding it would deny a capability that works.
 * 3. Files but zero **specs** is not an empty state at all. Pages, components and
 *    fixtures are real accumulated value; the tree renders, the Specs group is
 *    simply absent, and a one-line note in the tree column says so. Handled by
 *    `ProjectAutomationTab` — there is no component here for it, on purpose.
 *
 * Plus {@link NoFileSelected}, the right pane's resting state.
 *
 * None of these offers a Bootstrap/Adopt button: creating a repo by hand is ADR
 * 0014 slice 2 and out of scope, and a button that does nothing is worse than no
 * button. The one exit is the project's Runs tab — where an automation repo
 * actually comes from.
 */
export function NoRepoEmpty({ onViewRuns }: { onViewRuns: () => void }) {
  const { t } = useTranslation("projects");
  return (
    <div data-testid="automation-no-repo">
      <EmptyState
        icon={<FolderGit2 size={28} className="text-muted" />}
        title={t("automation.emptyTitle")}
        body={t("automation.emptyBody")}
        action={
          <Button variant="primary" onClick={onViewRuns}>
            {t("automation.viewRuns")}
          </Button>
        }
      />
    </div>
  );
}

/** The repo is scaffolded but nothing has been generated into it yet. */
export function ScaffoldOnlyEmpty({ onViewRuns }: { onViewRuns: () => void }) {
  const { t } = useTranslation("projects");
  return (
    <div data-testid="automation-scaffold-only">
      <EmptyState
        icon={<FileCode2 size={28} className="text-muted" />}
        title={t("automation.scaffoldOnlyTitle")}
        body={t("automation.scaffoldOnlyBody")}
        action={
          <Button variant="glass" onClick={onViewRuns}>
            {t("automation.viewRuns")}
          </Button>
        }
      />
    </div>
  );
}

/**
 * The right pane before anything is opened.
 *
 * A hint card rather than a blank column, and rather than auto-opening the first
 * file: content is fetched lazily per file (#768), so auto-opening would spend a
 * request on a file nobody asked for and defeat the tree/content split.
 *
 * Opaque surface, matching `ProjectFilePanel` — this text sits over the shell's
 * animated background, where a translucent card is genuinely hard to read.
 */
export function NoFileSelected() {
  const { t } = useTranslation("projects");
  return (
    <div
      className="flex flex-col items-center justify-center rounded-2xl border border-bd2 px-8 py-16 text-center"
      style={{ background: "var(--pop)", minHeight: 260 }}
      data-testid="automation-no-file-selected"
    >
      <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-card2">
        <MousePointerClick size={24} className="text-muted" />
      </div>
      <h3 className="m-0 mb-1.5 text-[15px] font-extrabold">
        {t("automation.selectFileTitle")}
      </h3>
      <p className="m-0 max-w-[380px] text-[12.5px] leading-relaxed text-txt4">
        {t("automation.selectFileBody")}
      </p>
    </div>
  );
}

/** Placeholder while a file's content is in flight. Mirrors the panel's shape
 *  (opaque card, header row, code block) so the swap has no layout jump. */
export function FileLoadingSkeleton() {
  return (
    <div
      className="overflow-hidden rounded-2xl border border-bd2"
      style={{ background: "var(--pop)" }}
      data-testid="automation-file-loading"
      aria-busy="true"
    >
      <div className="flex items-center gap-2.5 border-b border-bd3 px-4 py-3">
        <ShimmerBar className="h-3.5 w-[220px]" />
        <ShimmerBar className="h-3.5 w-[70px]" />
      </div>
      <div className="flex flex-col gap-2 px-4 py-4">
        {[92, 74, 84, 60, 78, 52, 88, 66].map((w, i) => (
          <ShimmerBar key={i} className="h-3" style={{ width: `${w}%` }} />
        ))}
      </div>
    </div>
  );
}

function ShimmerBar({
  className,
  style,
}: {
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <div
      className={`rounded bg-card3 ${className ?? ""}`}
      // `glowPulse` is the shell's existing opacity breathe (index.css); there is
      // no bare `pulse` keyframe to lean on, and adding one would duplicate it.
      style={{ animation: "glowPulse 1.6s ease-in-out infinite", ...style }}
    />
  );
}
