import { useState, type FormEvent } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Lock } from "lucide-react";
import { toast } from "@/lib/toast";
import { AuthLayout } from "@/components/auth/AuthLayout";
import { AuthLabel, PasswordInput } from "@/components/auth/fields";
import { api, ApiError } from "@/lib/api";

/**
 * Reset password (#76) — one public screen, reachable only with a `?token=…`.
 *
 * The screen used to open on a "forgot password" *request* form that asked for
 * an email and then claimed a reset link had been sent. No email is ever sent:
 * there is no mailer in this codebase, and `POST /auth/request-reset` says so
 * in its own comment. That form is gone (#673).
 *
 * The token half stays, because it is also how an **invited** user sets their
 * first password — the admin hands them this link out of band. Without a
 * token there is nothing to redeem, so the route sends the visitor to sign-in.
 */
export function ForgotPassword() {
  const [params] = useSearchParams();
  const token = params.get("token");
  return token ? <ResetForm token={token} /> : <Navigate to="/login" replace />;
}

const GRADIENT = "linear-gradient(135deg,#8b5cf6,#6366f1)";

/** Full-width violet gradient submit button with an inline loading spinner. */
function GradientButton({
  busy,
  busyLabel,
  children,
}: {
  busy: boolean;
  busyLabel: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="submit"
      disabled={busy}
      className="mt-[18px] flex h-[46px] w-full items-center justify-center gap-[9px] rounded-xl border-none text-[14.5px] font-bold text-white transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-80"
      style={{ background: GRADIENT, boxShadow: "0 10px 26px -8px rgba(139,92,246,.8)" }}
    >
      {busy ? (
        <>
          <span
            className="h-[17px] w-[17px] rounded-full border-2 border-white/40 border-t-white animate-spin-fast"
            aria-hidden
          />
          {busyLabel}
        </>
      ) : (
        children
      )}
    </button>
  );
}

function ResetForm({ token }: { token: string }) {
  const navigate = useNavigate();
  const { t } = useTranslation("auth");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [saving, setSaving] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (saving) return;
    if (password.length < 8) {
      toast.error(t("reset.tooShort"));
      return;
    }
    if (password !== confirm) {
      toast.error(t("reset.mismatch"));
      return;
    }
    setSaving(true);
    try {
      await api.auth.reset({ token, password });
      toast.success(t("reset.success"));
      navigate("/login");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : t("reset.error"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <AuthLayout>
      <div className="mb-6">
        <h2 className="m-0 mb-1.5 text-[26px] font-black tracking-[-0.02em]">{t("reset.title")}</h2>
        <p className="m-0 text-[13.5px] leading-relaxed text-muted">
          {t("reset.subtitle")}
        </p>
      </div>
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
        <div>
          <AuthLabel htmlFor="new-password">{t("reset.newPasswordLabel")}</AuthLabel>
          <PasswordInput
            id="new-password"
            autoComplete="new-password"
            autoFocus
            required
            icon={<Lock size={15} />}
            placeholder={t("reset.newPasswordPlaceholder")}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        <div>
          <AuthLabel htmlFor="confirm-password">{t("reset.confirmLabel")}</AuthLabel>
          <PasswordInput
            id="confirm-password"
            autoComplete="new-password"
            required
            icon={<Lock size={15} />}
            placeholder={t("reset.confirmPlaceholder")}
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        </div>
        <GradientButton busy={saving} busyLabel={t("reset.submitting")}>
          {t("reset.submit")}
        </GradientButton>
      </form>
    </AuthLayout>
  );
}
