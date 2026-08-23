import type { ProjectFile } from "@/types/api";

/**
 * The order the automation project's layers are shown in, mirroring #537 doc
 * §20's ownership model — business intent first (the spec the reviewer owns),
 * then app-UI knowledge (pages/components), then test setup (fixtures), scenario
 * input (data), and finally generic plumbing (utils/config).
 */
export const PROJECT_FILE_KINDS = [
  "spec",
  "page",
  "component",
  "fixture",
  "data",
  "util",
  "config",
] as const;

/** i18n key suffix for a kind's group label; unknown kinds fall into "other". */
export function kindLabelKey(kind: string): string {
  return (PROJECT_FILE_KINDS as readonly string[]).includes(kind) ? kind : "other";
}

export type ProjectFileGroup = { kind: string; files: ProjectFile[] };

/**
 * Group the project's files by `kind` in {@link PROJECT_FILE_KINDS} order,
 * dropping empty groups and collecting any unrecognised kind into a trailing
 * `other` group (so a newer server's kind still renders).
 *
 * Files inside a group are sorted by path so the list is stable across refetches.
 */
export function groupProjectFiles(files: ProjectFile[]): ProjectFileGroup[] {
  const byKind = new Map<string, ProjectFile[]>();
  for (const f of files) {
    const key = kindLabelKey(f.kind) === "other" ? "other" : f.kind;
    const bucket = byKind.get(key);
    if (bucket) bucket.push(f);
    else byKind.set(key, [f]);
  }
  const order = [...PROJECT_FILE_KINDS, "other"];
  return order
    .filter((k) => (byKind.get(k)?.length ?? 0) > 0)
    .map((kind) => ({
      kind,
      files: [...(byKind.get(kind) ?? [])].sort((a, b) => a.path.localeCompare(b.path)),
    }));
}

/** Last path segment of a project-relative path (`tests/X/Y.spec.ts` → `Y.spec.ts`). */
export function baseName(path: string): string {
  const i = path.lastIndexOf("/");
  return i === -1 ? path : path.slice(i + 1);
}

/**
 * The project-relative path to show for a spec's `filename`.
 *
 * Since #540 a project-backed spec's `filename` **is** the project-relative path
 * (`tests/<TICKET>/<TICKET>-TC-01.spec.ts`), so it is rendered as-is. A legacy
 * spec (`project_id IS NULL`) still carries a bare basename by design, and gets
 * the historical `tests/` prefix so it keeps rendering a sensible path (#606).
 */
export function specDisplayPath(filename: string | undefined): string {
  const name = filename ?? "";
  if (name === "") return "";
  return name.includes("/") ? name : `tests/${name}`;
}

/**
 * Build the file list for the selected spec: the project's own files, with a
 * synthetic entry for the **editable** spec guaranteed to be present and first.
 *
 * The server may or may not include the selected case's spec in `projectFiles`.
 * Either way exactly one row must represent the editable spec, so any entry whose
 * path matches `specFilename` is dropped and replaced by the synthetic one
 * (its code comes from `AutomationSpec.code`, which stays authoritative for the
 * editor, chat edits and the typewriter).
 *
 * Since #540 `specFilename` is itself a project-relative path, so the match is on
 * the **full path**, not the basename (#606) — a legacy bare filename is widened
 * to a path first so both shapes still resolve. `baseName` stays for display.
 *
 * @returns `null` when there is no project (legacy spec) — the caller then renders
 *   nothing at all, leaving the screen exactly as it was before #543.
 */
export function buildFileList(
  projectFiles: ProjectFile[] | undefined,
  specFilename: string | undefined,
): { specPath: string; groups: ProjectFileGroup[] } | null {
  if (projectFiles == null || projectFiles.length === 0) return null;
  const name = specDisplayPath(specFilename);
  const own = projectFiles.find((f) => f.path === name);
  const specPath = own?.path ?? name;
  const rest = projectFiles.filter((f) => f.path !== specPath);
  return {
    specPath,
    groups: groupProjectFiles([{ path: specPath, kind: "spec", code: "" }, ...rest]),
  };
}
