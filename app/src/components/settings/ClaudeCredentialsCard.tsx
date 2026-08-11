import { Check, File, KeyRound, Lock, ShieldCheck, Trash2, UploadCloud, Users } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Trans, useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";
import i18n from "@/i18n";
import { toast } from "@/lib/toast";
import { Pill } from "@/components/ui/badges";
import { ClaudeLogo, Spinner } from "@/components/ui/misc";
import {
  useClaudeCredentialsStatus,
  useHubClaudeCredential,
  useHubDataEnabled,
  useHubWebUrl,
  useDeleteOwnClaudeCredentials,
  useTestClaudeCredentials,
  useUploadOwnClaudeCredentials,
} from "@/hooks/queries";
import { cn } from "@/lib/cn";
import { relativeTime } from "@/screens/auth/profile/sessions";
import type { ClaudeCredentialsMeta, HubClaudeCredential } from "@/types/api";

/** "in N days"/"in N hours" for a future ISO timestamp; "Expired" once past;
 * "—" if absent. Exported so the admin shared-account card can reuse it. */
export function formatExpiry(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diffMs = then - Date.now();
  if (diffMs <= 0) return i18n.t("settings:expiry.expired");
  const hours = Math.round(diffMs / 3_600_000);
  if (hours < 24) return i18n.t("settings:expiry.inHours", { count: hours });
  const days = Math.round(hours / 24);
  return i18n.t("settings:expiry.inDays", { count: days });
}

/** True when a credential's metadata indicates the token is no longer usable: a
 * real call flagged it "expired", or its expiry is already past. Shared with the
 * top-bar AI-stats indicator so both surfaces agree. */
export function isCredentialExpired(meta: ClaudeCredentialsMeta | null | undefined): boolean {
  if (!meta) return false;
  if (meta.status === "expired") return true;
  if (meta.expiresAt) {
    const t = new Date(meta.expiresAt).getTime();
    if (!Number.isNaN(t) && t <= Date.now()) return true;
  }
  return false;
}

/**
 * Is the credential EmeHub resolved usable? (#512/#528)
 *
 * The load-bearing case is `status: "refreshable"`. A Claude OAuth **access**
 * token expires within hours, so a perfectly healthy hub credential is past its
 * `expiresAt` almost immediately and the hub reports `refreshable` — meaning it
 * holds a refresh token and will renew on use. Treating that as expired (as the
 * local `isCredentialExpired` check would, since it only looks at `expiresAt`)
 * paints a permanent amber warning over a working deployment.
 *
 * So: only an explicitly dead status is unhealthy. An unknown status on an
 * available credential reads as fine — the hub said it has one.
 */
export function isHubCredentialHealthy(cred: HubClaudeCredential | undefined): boolean {
  if (!cred?.available) return false;
  const status = (cred.status ?? "").toLowerCase();
  return status !== "expired" && status !== "invalid" && status !== "revoked";
}

/**
 * Expiry text for a hub credential, or `null` when quoting it would mislead.
 *
 * A refreshable credential's **access** token is routinely already past
 * `expiresAt` — the hub renews it on use — so printing "Expired" beside
 * "auto-renewing" states the opposite of the truth. A genuinely dead credential
 * still gets its "Expired".
 */
export function hubExpiryLabel(cred: HubClaudeCredential | undefined): string | null {
  if (!cred?.expiresAt) return null;
  const at = new Date(cred.expiresAt).getTime();
  if (Number.isNaN(at)) return null;
  if (at <= Date.now() && isHubCredentialHealthy(cred)) return null;
  return formatExpiry(cred.expiresAt);
}

/** Active/Expired chip driven by the credential's real status. */
function StatusPill({ meta }: { meta: ClaudeCredentialsMeta | null | undefined }) {
  const { t } = useTranslation("settings");
  return isCredentialExpired(meta) ? (
    <Pill color="#fbbf24" bg="rgba(251,191,36,.14)" dot>
      {t("credential.expired")}
    </Pill>
  ) : (
    <Pill color="#6ee7b7" bg="rgba(16,185,129,.14)" dot>
      {t("credential.active")}
    </Pill>
  );
}

/** "Test credential" button — runs a real minimal Claude call under the
 * effective credential and toasts the outcome. */
function TestCredentialButton() {
  const { t } = useTranslation("settings");
  const test = useTestClaudeCredentials();
  return (
    <button
      type="button"
      onClick={() =>
        test.mutate(undefined, {
          onSuccess: (r) => (r.ok ? toast.success(r.message) : toast.error(r.message)),
          onError: (e) => toast.error((e as Error).message || t("credential.testFailed")),
        })
      }
      disabled={test.isPending}
      className="flex w-full items-center justify-center gap-2 rounded-[11px] border border-white/[0.1] bg-white/[0.05] px-[15px] py-[9px] text-[12.5px] font-semibold text-[#dcdce4] transition-colors hover:bg-white/[0.1] disabled:opacity-50 sm:w-auto"
    >
      {test.isPending ? <Spinner size={14} /> : <ShieldCheck size={14} strokeWidth={2} />}
      {test.isPending ? t("status.testing") : t("credential.test")}
    </button>
  );
}

/** A file-picker `<label>` that also accepts drag-and-drop of a single file
 * (#95 item 4) — used by every credential upload/replace control that isn't
 * the big empty-state `UploadDropzone` below (which already had this).
 * Applies `dragClassName` on top of the caller's `className` while a file is
 * being dragged over it. Exported for reuse on the admin shared-account
 * screen's "Rotate / replace token" and "Add a shared Claude account"
 * controls. */
export function FileDropLabel({
  onFile,
  className,
  dragClassName,
  children,
}: {
  onFile: (file: File | undefined) => void;
  className: string;
  dragClassName: string;
  children: ReactNode;
}) {
  const [dragOver, setDragOver] = useState(false);
  return (
    <label
      className={cn(className, dragOver && dragClassName)}
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        onFile(e.dataTransfer.files[0]);
      }}
    >
      <input
        type="file"
        accept=".json,application/json"
        className="hidden"
        onChange={(e) => onFile(e.target.files?.[0])}
      />
      {children}
    </label>
  );
}

/** Small pill chips for a credential's OAuth scopes; "—" if none. Exported for
 * reuse on the admin shared-account card. */
export function ScopeChips({ scopes }: { scopes: string[] | null | undefined }) {
  if (!scopes || scopes.length === 0) return <span className="text-[13px] font-semibold">&#8212;</span>;
  return (
    <div className="flex flex-wrap gap-1">
      {scopes.map((s) => (
        <span
          key={s}
          className="rounded-full bg-white/[0.06] px-2 py-[2px] text-[10.5px] font-semibold text-[#c3c3d0]"
        >
          {s}
        </span>
      ))}
    </div>
  );
}

/** Reads a dropped/selected file's text contents (used for `.credentials.json`
 * uploads). Exported so the admin Claude-credentials screen can reuse it for
 * the shared-account upload. */
export function readFileText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(file);
  });
}

/** Labelled stat cell (e.g. "SUBSCRIPTION" → "—") used across the credential
 * detail grids. Exported for reuse on the admin shared-account card. */
export function Field({
  label,
  value,
  valueClassName,
}: {
  label: string;
  value: ReactNode;
  valueClassName?: string;
}) {
  return (
    <div>
      <div className="mb-[5px] text-[10.5px] font-bold tracking-[0.05em] text-[#6c6c7e]">{label}</div>
      <div className={cn("text-[13px] font-semibold", valueClassName)}>{value}</div>
    </div>
  );
}

/** Masked "ACCESS TOKEN" row shared by the personal and admin shared
 * credential cards. `GET /ai/credentials` never returns the token itself (by
 * design, for security), so "reveal" only toggles a fixed mask for an
 * explicit disclosure message — it never displays a real secret. */
export function AccessTokenRow({ accent = "#67e8f9" }: { accent?: string }) {
  const { t } = useTranslation("settings");
  const [revealed, setRevealed] = useState(false);
  return (
    <div className="mt-[14px]">
      <div className="mb-[7px] text-[10.5px] font-bold tracking-[0.05em] text-[#6c6c7e]">
        {t("credential.accessToken")}
      </div>
      <div className="flex items-center gap-[10px] rounded-[11px] border border-white/[0.08] bg-[rgba(8,8,13,.6)] px-[13px] py-[10px]">
        <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-[#c3c3d0]">
          {revealed ? t("credential.tokenNotExposed") : "•".repeat(28)}
        </span>
        <button
          type="button"
          onClick={() => setRevealed((r) => !r)}
          className="shrink-0 font-mono text-[11.5px] font-semibold"
          style={{ color: accent }}
        >
          {revealed ? t("credential.hide") : t("credential.show")}
        </button>
      </div>
    </div>
  );
}

function SourceCard({
  active,
  icon,
  iconBg,
  iconColor,
  title,
  description,
  onClick,
}: {
  active: boolean;
  icon: ReactNode;
  iconBg: string;
  iconColor: string;
  title: string;
  description: ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex-1 rounded-[14px] border p-[14px] text-left transition-colors",
        active
          ? "border-[rgba(139,92,246,.4)] bg-[rgba(139,92,246,.08)]"
          : "border-white/[0.08] bg-white/[0.03] hover:border-white/[0.16]",
      )}
    >
      <div className="mb-2 flex items-center gap-[9px]">
        <span
          className="flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-[9px]"
          style={{ background: iconBg, color: iconColor }}
        >
          {icon}
        </span>
        <span className="flex-1 text-[14.5px] font-extrabold tracking-[-0.01em]">{title}</span>
        {active && (
          <span
            className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full"
            style={{ background: "linear-gradient(135deg,#8b5cf6,#6366f1)" }}
          >
            <Check size={12} strokeWidth={3.2} color="#fff" />
          </span>
        )}
      </div>
      <div className="text-[12px] leading-[1.5] text-[#9a9aae]">{description}</div>
    </button>
  );
}

/** Read-only summary of the workspace's shared Claude account, backed by
 * `status.shared` (#95) — subscription/expiry/scopes from the uploaded
 * `.credentials.json`; "—" only when that field is genuinely absent. */
function SharedAccountCard({ meta }: { meta: ClaudeCredentialsMeta | null | undefined }) {
  const { t } = useTranslation("settings");
  return (
    <div className="rounded-[16px] border border-white/[0.08] bg-white/[0.03] p-4">
      <div className="flex items-center gap-3">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[12px] border border-[rgba(217,119,87,.3)] bg-[rgba(217,119,87,.16)]">
          <ClaudeLogo size={20} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[14px] font-bold">
            {meta?.accountEmail ?? t("credential.sharedAccount")}
          </div>
          <div className="truncate font-mono text-[12px] text-[#8b8b9e]">
            {meta?.accountOrg ?? t("credential.maintainedByAdmin")}
          </div>
        </div>
        <StatusPill meta={meta} />
      </div>
      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Field label={t("credential.fields.subscription")} value={meta?.subscriptionType ?? "—"} />
        <Field label={t("credential.fields.tokenExpires")} value={formatExpiry(meta?.expiresAt)} />
        <Field label={t("credential.fields.scopes")} value={<ScopeChips scopes={meta?.scopes} />} />
        <Field label={t("credential.fields.maintainedBy")} value={t("credential.workspaceAdmin")} valueClassName="text-[#c4b5fd]" />
      </div>
      <div className="mt-4 flex flex-col gap-[10px] border-t border-white/[0.06] pt-[14px] sm:flex-row sm:flex-wrap">
        <TestCredentialButton />
      </div>
      <div className="mt-3 flex items-center gap-2 text-[11.5px] text-[#7a7a8c]">
        <Lock size={13} strokeWidth={2} className="shrink-0" />
        <span>
          <Trans
            t={t}
            i18nKey="credential.switchToOwn"
            components={{ b: <b className="font-semibold text-[#a6a6b6]" /> }}
          />
        </span>
      </div>
    </div>
  );
}

function PersonalAccountCard({
  meta,
  uploading,
  onReplace,
  onRemove,
  removing,
}: {
  meta: ClaudeCredentialsMeta | null | undefined;
  uploading: boolean;
  onReplace: (file: File | undefined) => void;
  onRemove: () => void;
  removing: boolean;
}) {
  const { t } = useTranslation("settings");
  return (
    <div className="rounded-[16px] border border-[rgba(34,211,238,.22)] bg-white/[0.03] p-4">
      <div className="flex items-center gap-3">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[12px] border border-[rgba(34,211,238,.3)] bg-[rgba(34,211,238,.14)] text-[#67e8f9]">
          <File size={18} strokeWidth={2} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate font-mono text-[13.5px] font-bold">
            {meta?.accountEmail ?? ".credentials.json"}
          </div>
          <div className="truncate text-[11.5px] text-[#8b8b9e]">
            {meta?.accountOrg ?? t("credential.personalAccount")}
          </div>
        </div>
        <StatusPill meta={meta} />
      </div>
      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Field label={t("credential.fields.subscription")} value={meta?.subscriptionType ?? "—"} />
        <Field label={t("credential.fields.tokenExpires")} value={formatExpiry(meta?.expiresAt)} />
        <Field label={t("credential.fields.scopes")} value={<ScopeChips scopes={meta?.scopes} />} />
        <Field
          label={t("credential.fields.lastRefreshed")}
          value={meta?.lastRefreshed ? relativeTime(meta.lastRefreshed) : "—"}
        />
      </div>
      <AccessTokenRow />
      <div className="mt-4 flex flex-col gap-[10px] border-t border-white/[0.06] pt-[14px] sm:flex-row sm:flex-wrap">
        <TestCredentialButton />
        <FileDropLabel
          onFile={onReplace}
          className="flex w-full cursor-pointer items-center justify-center gap-2 rounded-[11px] border border-white/[0.1] bg-white/[0.05] px-[15px] py-[9px] text-[12.5px] font-semibold text-[#dcdce4] transition-colors hover:bg-white/[0.1] sm:w-auto"
          dragClassName="border-[rgba(34,211,238,.5)] bg-[rgba(34,211,238,.1)]"
        >
          <UploadCloud size={14} strokeWidth={2} />
          {uploading ? t("status.uploading") : t("credential.card.replaceFile")}
        </FileDropLabel>
        <button
          type="button"
          onClick={onRemove}
          disabled={removing}
          className="flex w-full items-center justify-center gap-2 rounded-[11px] border border-[rgba(244,63,94,.28)] bg-[rgba(244,63,94,.1)] px-[15px] py-[9px] text-[12.5px] font-semibold text-[#fb7185] transition-colors hover:bg-[rgba(244,63,94,.18)] disabled:opacity-50 sm:w-auto"
        >
          <Trash2 size={14} strokeWidth={2} />
          {removing ? t("status.removing") : t("credential.card.removeUseShared")}
        </button>
      </div>
    </div>
  );
}

function UploadDropzone({
  uploading,
  onFile,
  error,
}: {
  uploading: boolean;
  onFile: (file: File | undefined) => void;
  error: string | null;
}) {
  const { t } = useTranslation("settings");
  const [dragOver, setDragOver] = useState(false);
  return (
    <div>
      <label
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          onFile(e.dataTransfer.files[0]);
        }}
        className={cn(
          "flex cursor-pointer flex-col items-center gap-[9px] rounded-[16px] border-[1.5px] border-dashed p-[30px] text-center transition-colors",
          dragOver
            ? "border-[rgba(139,92,246,.6)] bg-[rgba(139,92,246,.09)]"
            : "border-[rgba(139,92,246,.35)] bg-[rgba(139,92,246,.05)]",
        )}
      >
        <input
          type="file"
          accept=".json,application/json"
          className="hidden"
          onChange={(e) => onFile(e.target.files?.[0])}
        />
        {uploading ? (
          <>
            <Spinner size={32} />
            <div className="text-[13.5px] font-bold">{t("status.readingToken")}</div>
            <div className="text-[11.5px] text-[#8b8b9e]">{t("credential.dropzone.parsing")}</div>
          </>
        ) : (
          <>
            <span className="flex h-11 w-11 items-center justify-center rounded-[13px] bg-[rgba(139,92,246,.14)] text-[#c4b5fd]">
              <UploadCloud size={22} strokeWidth={2} />
            </span>
            <div className="text-[14px] font-bold">
              <Trans
                t={t}
                i18nKey="credential.dropzone.dropTitle"
                components={{ code: <span className="font-mono text-[12.5px]" /> }}
              />
            </div>
            <div className="text-[12px] text-[#8b8b9e]">
              <Trans
                t={t}
                i18nKey="credential.dropzone.browse"
                components={{ code: <span className="font-mono text-[11px] text-[#a6a6b6]" /> }}
              />
            </div>
          </>
        )}
      </label>
      {error && <div className="mt-2 text-[12px] text-red-400">{error}</div>}
    </div>
  );
}

/** Claude CLI account management (#95, re-skinned per `design/…/Q-Agent.dc.html`
 * lines 1031–1098): a two-way chooser between the workspace's shared account
 * (admin-maintained, read-only here — see the dedicated admin screen) and the
 * signed-in user's own uploaded credential. Reuses the existing #95
 * upload/delete-own hooks; no backend changes.
 *
 * The chooser is view-only (which panel to show), not a stored preference —
 * the backend has no such field, and "own beats shared" is unconditional. It
 * defaults to whichever source is actually effective (`status.mode`), but can
 * be pinned via a `?claudeSource=shared|personal` query param so the AI
 * status popover's Shared/Personal shortcuts land on the right panel. */
export function ClaudeCredentialsCard() {
  const { t } = useTranslation("settings");
  const { data: status } = useClaudeCredentialsStatus();
  const { data: hubCred } = useHubClaudeCredential();
  const { enabled: hubData, resolved: hubDataResolved } = useHubDataEnabled();
  const uploadOwn = useUploadOwnClaudeCredentials();
  const deleteOwn = useDeleteOwnClaudeCredentials();

  const [searchParams] = useSearchParams();
  const requestedSource = searchParams.get("claudeSource");
  const [source, setSource] = useState<"shared" | "personal" | null>(
    requestedSource === "shared" || requestedSource === "personal" ? requestedSource : null,
  );
  const [fileError, setFileError] = useState<string | null>(null);

  useEffect(() => {
    if (source !== null || !status) return;
    setSource(status.mode === "own" ? "personal" : "shared");
  }, [status, source]);

  const activeSource = source ?? "shared";

  const handleFile = async (file: File | undefined) => {
    if (!file) return;
    setFileError(null);
    try {
      const contents = await readFileText(file);
      uploadOwn.mutate(
        { credentials: contents },
        {
          onSuccess: () => toast.success(t("credential.card.credentialsUpdated")),
          onError: () => toast.error(t("credential.card.credentialsSaveFailed")),
        },
      );
    } catch {
      setFileError(t("credential.card.readFailed"));
    }
  };

  // Don't decide what to render until `/health` has answered: showing the
  // Shared/Your-own picker for a frame and then withdrawing it is the same
  // "control that doesn't govern anything" defect, just briefer.
  if (!hubDataResolved) {
    return <div className="h-[180px] animate-pulse rounded-[16px] bg-white/[0.03]" />;
  }

  // EmeHub owns Claude credentials in this deployment (#528). Show what it
  // resolved, read-only, and hide the picker, the upload and the remove action —
  // none of them decides which account a run uses. The local credential is not
  // gone (it is still the server-side fallback when the hub can't be reached at
  // run start); it is simply not the user's to configure here.
  if (hubData) {
    return (
      <div>
        <p className="mb-[18px] text-[13.5px] leading-[1.6] text-[#a6a6b6]">
          {t("credential.card.hubIntro")}
        </p>
        <HubManagedCredential cred={hubCred} />
        <p className="text-[12.5px] text-[#8b8b9e]">{t("credential.card.hubLocalFallbackNote")}</p>
      </div>
    );
  }

  return (
    <div>
      <p className="mb-[18px] text-[13.5px] leading-[1.6] text-[#a6a6b6]">
        <Trans
          t={t}
          i18nKey="credential.card.intro"
          components={{
            code: (
              <span className="rounded-[5px] bg-[rgba(139,92,246,.14)] px-[6px] py-[1px] font-mono text-[12px] text-[#c4b5fd]" />
            ),
          }}
        />
      </p>

      {hubCred?.available ? <HubManagedCredential cred={hubCred} /> : null}

      {hubCred?.available ? (
        <p className="mb-2 text-[12.5px] text-[#8b8b9e]">{t("credential.card.localFallbackNote")}</p>
      ) : null}

      <div className="mb-[18px] flex gap-3">
        <SourceCard
          active={activeSource === "shared"}
          icon={<Users size={16} strokeWidth={2} />}
          iconBg="rgba(139,92,246,.16)"
          iconColor="#c4b5fd"
          title={t("credential.card.sharedTitle")}
          description={t("credential.card.sharedDescription")}
          onClick={() => setSource("shared")}
        />
        <SourceCard
          active={activeSource === "personal"}
          icon={<KeyRound size={16} strokeWidth={2} />}
          iconBg="rgba(34,211,238,.14)"
          iconColor="#67e8f9"
          title={t("credential.card.ownTitle")}
          description={
            <Trans
              t={t}
              i18nKey="credential.card.ownDescription"
              components={{ code: <span className="font-mono text-[11px]" /> }}
            />
          }
          onClick={() => setSource("personal")}
        />
      </div>

      {activeSource === "shared" ? (
        status?.hasShared ? (
          <SharedAccountCard meta={status.shared} />
        ) : (
          <div className="rounded-[16px] border border-white/[0.08] bg-white/[0.03] p-4 text-[13px] leading-[1.6] text-[#9a9aae]">
            <Trans
              t={t}
              i18nKey="credential.card.noShared"
              components={{ b: <b className="font-semibold text-[#c3c3d0]" /> }}
            />
          </div>
        )
      ) : status?.hasOwn ? (
        <PersonalAccountCard
          meta={status.own}
          uploading={uploadOwn.isPending}
          onReplace={handleFile}
          onRemove={() =>
            deleteOwn.mutate(undefined, {
              onSuccess: () =>
                toast.success(t("credential.card.credentialsRemoved")),
              onError: () => toast.error(t("credential.card.credentialsRemoveFailed")),
            })
          }
          removing={deleteOwn.isPending}
        />
      ) : (
        <UploadDropzone uploading={uploadOwn.isPending} onFile={handleFile} error={fileError} />
      )}
    </div>
  );
}


/**
 * The credential EmeHub resolves — what runs actually use (#512).
 *
 * Rendered above the local picker because it outranks it: with hub data on, a run
 * resolves from the hub and only falls back to the local credential when the hub
 * is unreachable. Before this, Settings showed a local account marked "Active"
 * that no run would use — the screen asserted something false, which is worse
 * than saying nothing.
 *
 * There is deliberately **no switch here.** The hub reserves credential mutation
 * to itself: `PUT /credentials/claude/mode` refuses an agent token (401, "Token is
 * not valid for this audience"), so offering a control that cannot work would be
 * a lie of a different kind. The deep link goes where the change can actually be
 * made.
 */
function HubManagedCredential({ cred }: { cred: HubClaudeCredential | undefined }) {
  const { t } = useTranslation("settings");
  const hubWebUrl = useHubWebUrl();
  // The hub being unreadable is not an error state (#491): the card still says
  // who owns the credential and where to manage it, it just can't quote today's
  // values. Blanking the surface would be the worse failure.
  const available = cred?.available === true;

  const sourceLabel =
    cred?.source === "own"
      ? t("credential.card.hubSourceOwn")
      : t("credential.card.hubSourceShared");

  return (
    <div
      className="mb-[18px] rounded-[16px] border p-4"
      style={{ background: "rgba(139,92,246,.07)", borderColor: "rgba(139,92,246,.28)" }}
    >
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <span className="text-[14px] font-bold text-ink">{t("credential.card.hubManagedTitle")}</span>
        {available ? (
          <span
            className="rounded-full px-2 py-[2px] text-[10.5px] font-bold uppercase tracking-wider"
            style={{ background: "rgba(52,211,153,.16)", color: "#6ee7b7" }}
          >
            {t("credential.card.hubEffective")}
          </span>
        ) : null}
      </div>
      <p className="m-0 mb-3 text-[12.5px] leading-relaxed text-[#a6a6b6]">
        {available
          ? t("credential.card.hubManagedBody")
          : t("credential.card.hubUnavailableBody")}
      </p>

      {available ? (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 text-[12.5px]">
          <span className="font-semibold text-ink">{cred?.label || sourceLabel}</span>
          <span className="text-[#8b8b9e]">{sourceLabel}</span>
          {cred?.subscriptionType ? (
            <span className="text-[#8b8b9e]">{cred.subscriptionType}</span>
          ) : null}
          {/* `refreshable` is the common live value — a Claude OAuth access token
              expires within hours — so it must read as usable, not expired. */}
          {cred?.status ? (
            <span className="text-[#8b8b9e]">
              {cred.status === "refreshable"
                ? t("credential.card.hubStatusRefreshable")
                : cred.status}
            </span>
          ) : null}
          {hubExpiryLabel(cred) ? (
            <span className="text-[#8b8b9e]">{hubExpiryLabel(cred)}</span>
          ) : null}
        </div>
      ) : null}

      {available && cred?.scopes?.length ? (
        <div className="mt-2.5">
          <ScopeChips scopes={cred.scopes} />
        </div>
      ) : null}

      {hubWebUrl ? (
        <a
          href={`${hubWebUrl}/app/claude`}
          target="_blank"
          rel="noreferrer"
          className="mt-3 inline-flex items-center gap-1.5 text-[12.5px] font-bold text-violet"
        >
          {t("credential.card.hubManageLink")}
        </a>
      ) : null}
    </div>
  );
}
