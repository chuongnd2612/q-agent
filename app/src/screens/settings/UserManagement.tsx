/**
 * Admin user management (#75, #94). Lists the workspace's users and lets an
 * admin invite new ones by email, flip their role (admin ↔ member),
 * activate/deactivate, and remove them — all via `api.auth.*` (ADR 0007).
 * Gated to `role === "admin"`; everyone else gets a "Not authorized" panel.
 *
 * Re-skinned to match the client design (`design/…/Q-Agent.dc.html`, lines
 * 1111–1230): stat cards, a USER/ROLE/CLAUDE CREDENTIAL/LAST ACTIVE/STATUS
 * table, and a per-row `⋯` menu. The "Claude credential" and "last active"
 * columns are backed by `GET /auth/users`' `credentialSource`/`lastActive`
 * fields (#95). Renders inside the app shell (child of `RequireAuth` → `App`).
 */

import { useEffect, useRef, useState, type FormEvent } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { AnimatePresence, motion } from "framer-motion";
import {
  Ban,
  Check,
  Copy,
  KeyRound,
  Lock,
  Mail,
  MoreVertical,
  Shield,
  Trash2,
  User as UserIcon,
  UserPlus,
  Users,
  X,
} from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "@/lib/toast";
import { AuthLabel, PasswordInput, TextInput } from "@/components/auth/fields";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { ApiError, api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useAuth } from "@/store/auth";
import { relativeTime } from "@/screens/auth/profile/sessions";
import type { AdminUser, User, UserRole } from "@/types/api";

/** Local (screen-scoped) query key — the shared `queryKeys` module is off-limits
 * to this slice, so we key the admin user list inline. */
const USERS_KEY = ["auth", "users"] as const;

const errMsg = (e: unknown, fallback: string) =>
  e instanceof ApiError || e instanceof Error ? e.message : fallback;

const initials = (u: User) =>
  `${u.firstName?.[0] ?? ""}${u.lastName?.[0] ?? ""}`.toUpperCase() ||
  u.email[0]?.toUpperCase() ||
  "?";

export function UserManagement() {
  const { t } = useTranslation("settings");
  const me = useAuth((s) => s.user);
  const qc = useQueryClient();
  const [inviteOpen, setInviteOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);

  const usersQuery = useQuery({
    queryKey: USERS_KEY,
    queryFn: () => api.auth.users(),
    enabled: me?.role === "admin",
  });

  const refresh = () => qc.invalidateQueries({ queryKey: USERS_KEY });

  // Row mutations share a single in-flight lock so a row's menu disables while
  // any action against it runs. We track the affected user id for feedback.
  const [busyId, setBusyId] = useState<number | null>(null);

  const updateMut = useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id: number;
      body: Partial<{ role: UserRole; isActive: boolean }>;
    }) => api.auth.updateUser(id, body),
    onMutate: (v) => setBusyId(v.id),
    onSuccess: (u) => {
      toast.success(t("users.updated", { name: `${u.firstName} ${u.lastName}`.trim() }));
      refresh();
    },
    onError: (e) => toast.error(errMsg(e, t("users.updateFailed"))),
    onSettled: () => setBusyId(null),
  });

  const removeMut = useMutation({
    mutationFn: (id: number) => api.auth.deleteUser(id),
    onMutate: (id) => setBusyId(id),
    onSuccess: () => {
      toast.success(t("users.removed"));
      refresh();
    },
    onError: (e) => toast.error(errMsg(e, t("users.removeFailed"))),
    onSettled: () => setBusyId(null),
  });

  // ── Admin gate ────────────────────────────────────────────────────────────
  if (me && me.role !== "admin") {
    return (
      <div className="mx-auto flex max-w-[560px] flex-col items-center py-24 text-center">
        <div className="mb-5 flex h-16 w-16 items-center justify-center rounded-2xl border border-white/10 bg-white/[0.03]">
          <Lock size={26} className="text-[#8b8b9e]" />
        </div>
        <h1 className="m-0 mb-2 text-[22px] font-black tracking-[-0.02em]">{t("admin.notAuthorizedTitle")}</h1>
        <p className="m-0 max-w-[380px] text-[13.5px] leading-relaxed text-muted">
          {t("users.notAuthorized")}
        </p>
      </div>
    );
  }

  const users = usersQuery.data ?? [];
  const stats = [
    { label: t("users.stats.total"), value: users.length },
    { label: t("users.stats.admins"), value: users.filter((u) => u.role === "admin").length },
    { label: t("users.stats.active"), value: users.filter((u) => u.isActive).length },
    { label: t("users.stats.inactive"), value: users.filter((u) => !u.isActive).length },
  ];

  return (
    <div className="mx-auto max-w-[1040px] px-4 py-10 md:px-0">
      {/* Header */}
      <div className="mb-[22px] flex flex-col gap-3 md:flex-row md:items-end md:justify-between md:gap-4">
        <div>
          <div className="mb-[5px] flex items-center gap-2 text-[13px] font-medium text-muted">
            <span className="rounded-full bg-[rgba(139,92,246,.16)] px-[7px] py-[2px] text-[9px] font-bold tracking-[.06em] text-[#c4b5fd]">
              {t("admin.badge")}
            </span>
            {t("admin.workspace")}
          </div>
          <h1 className="m-0 text-[28px] font-black tracking-[-0.03em]">{t("users.title")}</h1>
        </div>
        <div className="flex items-center gap-2.5">
          <Button variant="glass" className="flex-1 md:flex-none" onClick={() => setCreateOpen(true)}>
            <UserIcon size={15} strokeWidth={2.4} />
            {t("users.createUser")}
          </Button>
          <Button variant="primary" className="flex-1 md:flex-none" onClick={() => setInviteOpen(true)}>
            <UserPlus size={15} strokeWidth={2.4} />
            {t("users.inviteUser")}
          </Button>
        </div>
      </div>

      {/* Stat cards */}
      <div className="mb-[22px] grid grid-cols-2 gap-3 sm:grid-cols-4">
        {stats.map((st) => (
          <div
            key={st.label}
            className="rounded-2xl border border-white/[0.07] bg-white/[0.035] p-[16px_18px]"
          >
            <div className="text-[26px] font-black tracking-[-0.02em]">
              {usersQuery.isLoading ? "–" : st.value}
            </div>
            <div className="mt-[3px] text-[12px] text-muted">{st.label}</div>
          </div>
        ))}
      </div>

      {/* States */}
      {usersQuery.isError ? (
        <div className="rounded-2xl border border-[rgba(244,63,94,.28)] bg-[rgba(244,63,94,.08)] p-6 text-[13.5px] text-[#fb7185]">
          {errMsg(usersQuery.error, t("users.loadFailed"))}
        </div>
      ) : usersQuery.isLoading ? (
        <div className="flex flex-col gap-2.5">
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              className="h-[64px] animate-pulse rounded-[14px] border border-white/[0.06] bg-white/[0.02]"
            />
          ))}
        </div>
      ) : users.length === 0 ? (
        <div className="rounded-2xl border border-white/[0.07] bg-panel/60 p-10 text-center text-[13.5px] text-muted">
          {t("users.empty")}
        </div>
      ) : (
        <>
          {/* Table header */}
          <div className="hidden items-center gap-3.5 px-4 pb-[10px] text-[10px] font-bold tracking-[.06em] text-[#6c6c7e] sm:flex">
            <span className="flex-1">{t("users.cols.user")}</span>
            <span className="w-[104px] shrink-0">{t("users.cols.role")}</span>
            <span className="hidden w-[168px] shrink-0 md:block">{t("users.cols.credential")}</span>
            <span className="hidden w-[120px] shrink-0 md:block">{t("users.cols.lastActive")}</span>
            <span className="w-[100px] shrink-0">{t("users.cols.status")}</span>
            <span className="w-[30px] shrink-0" />
          </div>

          <div className="flex flex-col gap-[9px]">
            {users.map((u) => {
              const isMe = me?.id === u.id;
              const busy = busyId === u.id;
              return (
                <div
                  key={u.id}
                  className={cn(
                    "flex flex-wrap items-center gap-2.5 rounded-[14px] border border-white/[0.07] bg-white/[0.035] p-[13px_16px] sm:flex-nowrap sm:gap-3.5",
                    !u.isActive && "opacity-50",
                  )}
                >
                  {/* Identity */}
                  <div className="flex min-w-0 flex-1 items-center gap-3">
                    <div
                      className={cn(
                        "flex h-[38px] w-[38px] shrink-0 items-center justify-center rounded-[11px] text-[13.5px] font-bold text-white",
                        u.isActive ? "accent-gradient" : "bg-white/10 text-[#8b8b9e]",
                      )}
                    >
                      {initials(u)}
                    </div>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-[13.5px] font-bold">
                          {`${u.firstName} ${u.lastName}`.trim() || u.email}
                        </span>
                        {isMe && (
                          <span className="shrink-0 rounded-full bg-white/[0.08] px-2 py-0.5 text-[10px] font-semibold text-muted">
                            {t("users.you")}
                          </span>
                        )}
                      </div>
                      <div className="truncate text-[11.5px] text-muted">{u.email}</div>
                    </div>
                  </div>

                  {/* Role — always visible (wraps to its own line on very narrow phones
                      instead of hiding); fixed column from sm+ (unchanged) */}
                  <div className="shrink-0 sm:w-[104px]">
                    <RoleBadge role={u.role} />
                  </div>

                  {/* Claude credential (#95) */}
                  <div className="hidden w-[168px] shrink-0 md:block">
                    <CredentialSourceCell source={u.credentialSource} />
                  </div>

                  {/* Last active (#95) */}
                  <div className="hidden w-[120px] shrink-0 text-[12px] text-[#9a9aae] md:block">
                    {u.lastActive ? relativeTime(u.lastActive) : t("users.never")}
                  </div>

                  {/* Status — always visible (wraps alongside Role on very narrow phones
                      instead of hiding); fixed column from sm+ (unchanged) */}
                  <div className="shrink-0 sm:w-[100px]">
                    <StatusBadge active={u.isActive} />
                  </div>

                  {/* Actions */}
                  <div className="w-[30px] shrink-0">
                    <UserActionsMenu
                      user={u}
                      disabled={isMe}
                      busy={busy}
                      onSetRole={(role) => updateMut.mutate({ id: u.id, body: { role } })}
                      onSetActive={(isActive) => updateMut.mutate({ id: u.id, body: { isActive } })}
                      onRemove={() => removeMut.mutate(u.id)}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}

      {inviteOpen && (
        <InviteUserModal
          onClose={() => setInviteOpen(false)}
          // Refresh only — the modal stays open on success to show the
          // set-password link, which is the invitee's only way in (#673).
          onInvited={refresh}
        />
      )}

      {createOpen && (
        <CreateUserModal
          onClose={() => setCreateOpen(false)}
          onCreated={() => {
            setCreateOpen(false);
            refresh();
          }}
        />
      )}
    </div>
  );
}

function RoleBadge({ role }: { role: UserRole }) {
  const { t } = useTranslation("settings");
  const admin = role === "admin";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-[11px] py-[5px] text-[11.5px] font-bold",
        admin ? "bg-[rgba(139,92,246,.16)] text-[#c4b5fd]" : "bg-white/[0.06] text-[#c3c3d0]",
      )}
    >
      {admin && <Shield size={11} />}
      {admin ? t("users.roleAdmin") : t("users.roleMember")}
    </span>
  );
}

/** "Claude credential" cell (#95): cyan token icon for a personal upload,
 * an orange dot + label for the shared fallback, "—" for none. */
function CredentialSourceCell({ source }: { source: AdminUser["credentialSource"] }) {
  const { t } = useTranslation("settings");
  if (source === "personal") {
    return (
      <span className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-[#67e8f9]">
        <KeyRound size={13} strokeWidth={2} />
        {t("users.credentialPersonal")}
      </span>
    );
  }
  if (source === "shared") {
    return (
      <span className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-[#e0a58c]">
        <span className="h-[6px] w-[6px] shrink-0 rounded-full bg-[#d97757]" />
        {t("users.credentialShared")}
      </span>
    );
  }
  return <span className="text-[12px] text-[#6c6c7e]">&#8212;</span>;
}

function StatusBadge({ active }: { active: boolean }) {
  const { t } = useTranslation("settings");
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-[10px] py-[4px] text-[11px] font-bold",
        active ? "bg-[rgba(16,185,129,.14)] text-[#6ee7b7]" : "bg-[rgba(148,163,184,.13)] text-[#8b93a7]",
      )}
    >
      {active ? t("users.statusActive") : t("users.statusDeactivated")}
    </span>
  );
}

const MENU_WIDTH = 196;

/** Per-row `⋯` action menu — role toggle, activate/deactivate, remove.
 * Portalled to `document.body` with fixed positioning anchored to the
 * trigger's bounding rect, per the project's floating-overlay rule (the row
 * has no transform of its own here, but the pattern is kept consistent with
 * `RunActionsMenu`). */
function UserActionsMenu({
  user,
  disabled,
  busy,
  onSetRole,
  onSetActive,
  onRemove,
}: {
  user: User;
  /** True for the signed-in admin's own row — self-service role/status/removal
   * changes are blocked in this menu (the backend also enforces the
   * last-active-admin lockout on role/deactivate). */
  disabled: boolean;
  busy: boolean;
  onSetRole: (role: UserRole) => void;
  onSetActive: (active: boolean) => void;
  onRemove: () => void;
}) {
  const { t } = useTranslation("settings");
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const [confirmingRemove, setConfirmingRemove] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const place = () => {
      const el = btnRef.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      setPos({
        top: r.bottom + 6,
        left: Math.max(12, Math.min(r.right - MENU_WIDTH, window.innerWidth - MENU_WIDTH - 12)),
      });
    };
    place();
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (btnRef.current?.contains(t)) return;
      if ((e.target as HTMLElement).closest?.("[data-user-actions-menu]")) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const reposition = () => place();
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", reposition, true);
      window.removeEventListener("resize", reposition);
    };
  }, [open]);

  const menuItemClass =
    "flex w-full items-center gap-[9px] rounded-[9px] px-2.5 py-2 text-left text-[12.5px] font-semibold text-[#dcdce4] hover:bg-white/[0.06] disabled:cursor-not-allowed disabled:opacity-40";

  return (
    <span className="contents" onClick={(e) => e.stopPropagation()}>
      <button
        ref={btnRef}
        type="button"
        title={disabled ? t("users.actionsSelf") : t("users.actions")}
        aria-label={t("users.actions")}
        disabled={disabled || busy}
        onClick={() => setOpen((o) => !o)}
        className="flex h-[30px] w-[30px] items-center justify-center rounded-[9px] border border-white/[0.09] bg-white/[0.04] text-[#8b8b9e] transition-colors hover:bg-white/[0.09] hover:text-[#dcdce4] disabled:cursor-not-allowed disabled:opacity-30"
      >
        <MoreVertical size={16} />
      </button>

      {createPortal(
        <AnimatePresence>
          {open && pos && (
            <motion.div
              data-user-actions-menu
              initial={{ opacity: 0, y: -6, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -6, scale: 0.98 }}
              transition={{ duration: 0.14 }}
              className="fixed z-[1000] rounded-xl border border-white/[0.12] p-1.5 shadow-[0_24px_60px_-18px_rgba(0,0,0,1)]"
              style={{ top: pos.top, left: pos.left, width: MENU_WIDTH, background: "rgba(26,26,34,.98)" }}
              onMouseDown={(e) => e.stopPropagation()}
            >
              {user.role === "admin" ? (
                <button
                  type="button"
                  onClick={() => {
                    setOpen(false);
                    onSetRole("member");
                  }}
                  className={menuItemClass}
                >
                  <UserIcon size={14} />
                  {t("users.changeToMember")}
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    setOpen(false);
                    onSetRole("admin");
                  }}
                  className={menuItemClass}
                >
                  <Shield size={14} />
                  {t("users.makeAdmin")}
                </button>
              )}
              {user.isActive ? (
                <button
                  type="button"
                  onClick={() => {
                    setOpen(false);
                    onSetActive(false);
                  }}
                  className={menuItemClass}
                >
                  <Ban size={14} />
                  {t("users.deactivate")}
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    setOpen(false);
                    onSetActive(true);
                  }}
                  className={menuItemClass}
                >
                  <Check size={14} />
                  {t("users.reactivate")}
                </button>
              )}
              <div className="my-1 h-px bg-white/[0.08]" />
              <button
                type="button"
                onClick={() => {
                  setOpen(false);
                  setConfirmingRemove(true);
                }}
                className={cn(menuItemClass, "text-[#fb7185] hover:bg-[rgba(244,63,94,.12)]")}
              >
                <Trash2 size={14} />
                {t("users.removeUser")}
              </button>
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}

      <ConfirmDialog
        open={confirmingRemove}
        title={t("users.removeTitle")}
        message={t("users.removeMessage", { name: `${user.firstName} ${user.lastName}`.trim() || user.email })}
        confirmLabel={t("users.removeUser")}
        danger
        loading={busy}
        onConfirm={onRemove}
        onClose={() => setConfirmingRemove(false)}
      />
    </span>
  );
}

/** Invite-user modal — email + role only, matching the design
 * (`Q-Agent.dc.html` lines 1217–1230). Portalled to `document.body` with
 * fixed positioning per the project overlay rule. Calls `inviteUser` (#94).
 *
 * The invited account is created with an empty `password_hash`, so it is
 * unusable until the set-password token is redeemed at `/forgot?token=…` —
 * and nothing emails that link, because there is no mailer (#673). So on
 * success the modal does not close: it shows the link for the admin to copy
 * and send. Closing without copying it strands the account. */
function InviteUserModal({
  onClose,
  onInvited,
}: {
  onClose: () => void;
  onInvited: () => void;
}) {
  const { t } = useTranslation("settings");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<UserRole>("member");
  // Set once the invite lands: the created address plus the absolute
  // set-password URL. Its presence switches the modal to the success view.
  const [issued, setIssued] = useState<{ email: string; link: string } | null>(null);
  const [copied, setCopied] = useState(false);

  const inviteMut = useMutation({
    mutationFn: () => api.auth.inviteUser({ email: email.trim(), role }),
    onSuccess: ({ user: u, resetToken }) => {
      toast.success(t("users.invite.invited", { email: u.email }));
      setIssued({
        email: u.email,
        link: `${window.location.origin}/forgot?token=${encodeURIComponent(resetToken)}`,
      });
      onInvited();
    },
    onError: (e) => toast.error(errMsg(e, t("users.invite.sendFailed"))),
  });

  const copyLink = async () => {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.link);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error(t("users.invite.copyFailed"));
    }
  };

  const canSubmit = /.+@.+\..+/.test(email.trim()) && !inviteMut.isPending;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    inviteMut.mutate();
  };

  return createPortal(
    <div
      onClick={onClose}
      className="fixed inset-0 z-[100] flex items-center justify-center p-6"
      style={{ background: "rgba(6,6,10,.62)" }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[min(430px,100%)] rounded-[20px] border border-white/[0.12] p-6"
        style={{ background: "#15151c", boxShadow: "0 40px 90px -30px #000", animation: "fadeInUp .25s ease both" }}
      >
        <div className="mb-[18px] flex items-center gap-3">
          <div className="accent-gradient flex h-[38px] w-[38px] shrink-0 items-center justify-center rounded-[11px]">
            {issued ? (
              <KeyRound size={19} color="#fff" strokeWidth={2.2} />
            ) : (
              <UserPlus size={19} color="#fff" strokeWidth={2.2} />
            )}
          </div>
          <div className="flex-1">
            <h2 className="m-0 text-[18px] font-black tracking-[-0.02em]">
              {issued ? t("users.invite.linkTitle") : t("users.invite.title")}
            </h2>
            <p className="m-0 mt-0.5 text-[12.5px] text-muted">
              {issued ? t("users.invite.linkSubtitle", { email: issued.email }) : t("users.invite.subtitle")}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("common:close")}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-dim transition-colors hover:bg-white/[0.08] hover:text-ink"
          >
            <X size={17} />
          </button>
        </div>

        {issued ? (
          <div>
            <AuthLabel htmlFor="iu-link">{t("users.invite.linkLabel")}</AuthLabel>
            <div className="flex items-center gap-2">
              <input
                id="iu-link"
                readOnly
                value={issued.link}
                onFocus={(e) => e.currentTarget.select()}
                className="h-[42px] min-w-0 flex-1 rounded-xl border border-white/[0.12] bg-black/25 px-3 font-mono text-[12px] text-ink outline-none"
              />
              <Button type="button" variant="glass" onClick={copyLink}>
                {copied ? <Check size={15} /> : <Copy size={15} />}
                {copied ? t("users.invite.copied") : t("users.invite.copy")}
              </Button>
            </div>
            <p className="mb-0 mt-2.5 text-[11.5px] leading-relaxed text-faint">
              {t("users.invite.linkHint")}
            </p>
            <div className="mt-5 flex items-center justify-end">
              <Button type="button" variant="primary" onClick={onClose}>
                {t("users.invite.done")}
              </Button>
            </div>
          </div>
        ) : (
        <form onSubmit={submit}>
          <div className="mb-4">
            <AuthLabel htmlFor="iu-email">{t("users.invite.emailLabel")}</AuthLabel>
            <TextInput
              id="iu-email"
              type="email"
              icon={<Mail size={15} />}
              placeholder={t("users.invite.emailPlaceholder")}
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>

          <div className="mb-5">
            <AuthLabel>{t("users.invite.roleLabel")}</AuthLabel>
            <div className="flex gap-2 rounded-xl bg-black/25 p-1">
              {(["member", "admin"] as const).map((r) => {
                const on = role === r;
                return (
                  <button
                    key={r}
                    type="button"
                    onClick={() => setRole(r)}
                    className={cn(
                      "flex-1 rounded-[10px] py-[10px] text-[13px] font-bold transition-colors",
                      on ? "accent-gradient text-white" : "bg-white/[0.05] text-[#9a9aae]",
                    )}
                  >
                    {r === "admin" ? t("users.roleAdmin") : t("users.roleMember")}
                  </button>
                );
              })}
            </div>
            <p className="mb-0 mt-2 text-[11.5px] text-faint">
              {t("users.invite.roleHint")}
            </p>
          </div>

          <div className="flex items-center justify-end gap-2.5">
            <Button type="button" variant="glass" onClick={onClose}>
              {t("common:cancel")}
            </Button>
            <Button type="submit" variant="primary" disabled={!canSubmit}>
              {inviteMut.isPending ? t("status.sending") : t("users.invite.send")}
            </Button>
          </div>
        </form>
        )}
      </div>
    </div>,
    document.body,
  );
}

/** Manual "Create user" modal — same shell/styling as `InviteUserModal`
 * (design's invite modal, `Q-Agent.dc.html` lines 1217–1230) plus the extra
 * name + password fields `api.auth.createUser` needs. Unlike invite, this
 * creates the account with a usable password immediately — no reset-token
 * email flow. Portalled to `document.body` with fixed positioning per the
 * project's floating-overlay rule. */
function CreateUserModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const { t } = useTranslation("settings");
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<UserRole>("member");

  const createMut = useMutation({
    mutationFn: () =>
      api.auth.createUser({
        email: email.trim(),
        firstName: firstName.trim(),
        lastName: lastName.trim(),
        role,
        password,
      }),
    onSuccess: (u) => {
      toast.success(t("users.create.created", { email: u.email }));
      onCreated();
    },
    onError: (e) => toast.error(errMsg(e, t("users.create.createFailed"))),
  });

  const canSubmit =
    /.+@.+\..+/.test(email.trim()) && firstName.trim().length > 0 && password.length >= 8 && !createMut.isPending;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    createMut.mutate();
  };

  return createPortal(
    <div
      onClick={onClose}
      className="fixed inset-0 z-[100] flex items-center justify-center p-6"
      style={{ background: "rgba(6,6,10,.62)" }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[min(430px,100%)] rounded-[20px] border border-white/[0.12] p-6"
        style={{ background: "#15151c", boxShadow: "0 40px 90px -30px #000", animation: "fadeInUp .25s ease both" }}
      >
        <div className="mb-[18px] flex items-center gap-3">
          <div className="accent-gradient flex h-[38px] w-[38px] shrink-0 items-center justify-center rounded-[11px]">
            <UserIcon size={19} color="#fff" strokeWidth={2.2} />
          </div>
          <div className="flex-1">
            <h2 className="m-0 text-[18px] font-black tracking-[-0.02em]">{t("users.create.title")}</h2>
            <p className="m-0 mt-0.5 text-[12.5px] text-muted">
              {t("users.create.subtitle")}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("common:close")}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-dim transition-colors hover:bg-white/[0.08] hover:text-ink"
          >
            <X size={17} />
          </button>
        </div>

        <form onSubmit={submit}>
          <div className="mb-4 flex gap-3">
            <div className="flex-1">
              <AuthLabel htmlFor="cu-first">{t("users.create.firstNameLabel")}</AuthLabel>
              <TextInput
                id="cu-first"
                placeholder={t("users.create.firstNamePlaceholder")}
                autoFocus
                value={firstName}
                onChange={(e) => setFirstName(e.target.value)}
              />
            </div>
            <div className="flex-1">
              <AuthLabel htmlFor="cu-last">{t("users.create.lastNameLabel")}</AuthLabel>
              <TextInput
                id="cu-last"
                placeholder={t("users.create.lastNamePlaceholder")}
                value={lastName}
                onChange={(e) => setLastName(e.target.value)}
              />
            </div>
          </div>

          <div className="mb-4">
            <AuthLabel htmlFor="cu-email">{t("users.create.emailLabel")}</AuthLabel>
            <TextInput
              id="cu-email"
              type="email"
              icon={<Mail size={15} />}
              placeholder={t("users.create.emailPlaceholder")}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>

          <div className="mb-4">
            <AuthLabel htmlFor="cu-password">{t("users.create.passwordLabel")}</AuthLabel>
            <PasswordInput
              id="cu-password"
              placeholder={t("users.create.passwordPlaceholder")}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>

          <div className="mb-5">
            <AuthLabel>{t("users.invite.roleLabel")}</AuthLabel>
            <div className="flex gap-2 rounded-xl bg-black/25 p-1">
              {(["member", "admin"] as const).map((r) => {
                const on = role === r;
                return (
                  <button
                    key={r}
                    type="button"
                    onClick={() => setRole(r)}
                    className={cn(
                      "flex-1 rounded-[10px] py-[10px] text-[13px] font-bold transition-colors",
                      on ? "accent-gradient text-white" : "bg-white/[0.05] text-[#9a9aae]",
                    )}
                  >
                    {r === "admin" ? t("users.roleAdmin") : t("users.roleMember")}
                  </button>
                );
              })}
            </div>
            <p className="mb-0 mt-2 text-[11.5px] text-faint">
              {t("users.invite.roleHint")}
            </p>
          </div>

          <div className="flex items-center justify-end gap-2.5">
            <Button type="button" variant="glass" onClick={onClose}>
              {t("common:cancel")}
            </Button>
            <Button type="submit" variant="primary" disabled={!canSubmit}>
              {createMut.isPending ? t("status.creating") : t("users.createUser")}
            </Button>
          </div>
        </form>
      </div>
    </div>,
    document.body,
  );
}
