/**
 * App toast — a faithful port of `design/Toast (standalone).html`: a dark glass
 * card with an accent icon box (animated draw-on check for success, alert glyph
 * for error/message), title + optional subtitle, a dismiss button, and an
 * auto-dismiss progress bar.
 *
 * We keep `sonner` as the engine (queue, positioning, timing, stacking) but
 * render EVERY toast via `toast.custom`, so call sites keep the familiar
 * `toast.success(msg, { description })` / `toast.error(...)` API — they just
 * import `toast` from here instead of from "sonner". The keyframes live in
 * `index.css` (`qtoastIn` / `qtoastBar` / `qtoastCircle` / `qtoastCheck`).
 */
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { toast as sonner, type ExternalToast } from "sonner";

type QType = "success" | "error" | "message";

/** Design default (design uses a 3.6s auto-dismiss). */
const DEFAULT_DURATION = 3600;

const ACCENTS: Record<QType, { color: string; bg: string; border: string }> = {
  success: { color: "#34d399", bg: "rgba(52,211,153,.14)", border: "rgba(52,211,153,.32)" },
  error: { color: "#f87171", bg: "rgba(248,113,113,.14)", border: "rgba(248,113,113,.32)" },
  message: { color: "#a78bfa", bg: "rgba(139,92,246,.14)", border: "rgba(139,92,246,.32)" },
};

/** Success: circle + tick drawn on (52-viewBox). Error/message: a static
 * alert/info glyph in a 24-viewBox. All stroke the accent color. */
function ToastIcon({ type, accent }: { type: QType; accent: string }) {
  if (type === "success") {
    return (
      <svg width={24} height={24} viewBox="0 0 52 52" style={{ flexShrink: 0 }}>
        <circle
          cx={26}
          cy={26}
          r={23}
          fill="none"
          stroke={accent}
          strokeWidth={3}
          strokeLinecap="round"
          strokeDasharray={145}
          strokeDashoffset={145}
          style={{ animation: "qtoastCircle .55s cubic-bezier(.65,0,.35,1) forwards" }}
        />
        <path
          d="M15.5 27l6.8 6.8L37 19"
          fill="none"
          stroke={accent}
          strokeWidth={3.6}
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeDasharray={40}
          strokeDashoffset={40}
          style={{ animation: "qtoastCheck .3s cubic-bezier(.65,0,.35,1) .5s forwards" }}
        />
      </svg>
    );
  }
  return (
    <svg
      width={17}
      height={17}
      viewBox="0 0 24 24"
      fill="none"
      stroke={accent}
      strokeWidth={2.3}
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ flexShrink: 0 }}
    >
      <circle cx={12} cy={12} r={9} />
      {/* error = exclamation (line then dot); message = info "i" (dot then line) */}
      <path d={type === "error" ? "M12 8v5M12 16.4v.01" : "M12 8.4v.01M12 12v4"} />
    </svg>
  );
}

/** A toast's optional call to action. Rendered inline — `sonner.custom` draws
 *  ONLY the node it is given, so an `action` passed in the options object is
 *  silently dropped. The Execution screen's "pair a Local Agent" CTA had been
 *  passing one since it was written and it never appeared on screen (#643). */
interface ToastAction {
  label: ReactNode;
  onClick: () => void;
}

function QToast({
  id,
  type,
  title,
  sub,
  duration,
  action,
}: {
  id: number | string;
  type: QType;
  title: ReactNode;
  sub?: ReactNode;
  duration: number;
  action?: ToastAction;
}) {
  const a = ACCENTS[type];
  const { t } = useTranslation("commands");
  return (
    <div
      style={{
        position: "relative",
        overflow: "hidden",
        display: "flex",
        alignItems: "center",
        gap: 12,
        minWidth: 264,
        maxWidth: 390,
        padding: "11px 12px 11px 13px",
        borderRadius: 14,
        background: "rgba(22,22,30,.92)",
        backdropFilter: "blur(26px) saturate(1.4)",
        WebkitBackdropFilter: "blur(26px) saturate(1.4)",
        border: "1px solid rgba(255,255,255,.1)",
        boxShadow: "0 22px 55px -16px rgba(0,0,0,.85), inset 0 1px 0 rgba(255,255,255,.05)",
        fontFamily: "'Satoshi',system-ui,sans-serif",
        animation: "qtoastIn .5s cubic-bezier(.2,.9,.25,1) both",
      }}
    >
      <div
        style={{
          position: "relative",
          width: 32,
          height: 32,
          flexShrink: 0,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          borderRadius: 9,
          background: a.bg,
          border: `1px solid ${a.border}`,
        }}
      >
        <ToastIcon type={type} accent={a.color} />
      </div>

      <div style={{ flex: "1 1 0%", minWidth: 0, display: "flex", flexDirection: "column", gap: 2 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: "#f4f4f8", lineHeight: 1.25 }}>
          {title}
        </span>
        {sub ? (
          <span style={{ fontSize: 11.5, color: "#9a9aac", lineHeight: 1.3 }}>{sub}</span>
        ) : null}
      </div>

      {action ? (
        <button
          type="button"
          onClick={() => {
            action.onClick();
            sonner.dismiss(id);
          }}
          style={{
            flexShrink: 0,
            alignSelf: "center",
            padding: "5px 10px",
            borderRadius: 9,
            fontSize: 11.5,
            fontWeight: 700,
            cursor: "pointer",
            color: a.color,
            background: a.bg,
            border: `1px solid ${a.border}`,
          }}
        >
          {action.label}
        </button>
      ) : null}

      <button
        type="button"
        aria-label={t("toast.dismiss")}
        onClick={() => sonner.dismiss(id)}
        style={{
          flexShrink: 0,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          width: 24,
          height: 24,
          borderRadius: 7,
          background: "transparent",
          border: "none",
          cursor: "pointer",
          color: "#6c6c7e",
          transition: "background .15s, color .15s",
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.background = "rgba(255,255,255,.08)";
          e.currentTarget.style.color = "#c3c3d0";
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.background = "transparent";
          e.currentTarget.style.color = "#6c6c7e";
        }}
      >
        <svg width={13} height={13} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round">
          <path d="M6 6l12 12M18 6 6 18" />
        </svg>
      </button>

      <span
        style={{
          position: "absolute",
          left: 0,
          bottom: 0,
          height: 2.5,
          width: "100%",
          transformOrigin: "left center",
          background: a.color,
          opacity: 0.55,
          animation: `qtoastBar ${duration}ms linear both`,
        }}
      />
    </div>
  );
}

function show(type: QType, title: ReactNode, opts?: ExternalToast) {
  const duration = typeof opts?.duration === "number" ? opts.duration : DEFAULT_DURATION;
  // sonner allows `description` to be a render function — resolve it to a node.
  const sub = typeof opts?.description === "function" ? opts.description() : opts?.description;
  // Pull `action` out of the options and hand it to QToast: sonner renders only
  // our node, so leaving it in `opts` means it is never drawn (#643).
  const action = opts?.action as ToastAction | undefined;
  return sonner.custom(
    (id) => (
      <QToast
        id={id}
        type={type}
        title={title}
        sub={sub}
        duration={duration}
        action={action && typeof action.onClick === "function" ? action : undefined}
      />
    ),
    { ...opts, duration },
  );
}

/** Drop-in replacement for sonner's `toast`, styled to the Q-Agent design.
 * Supports the shapes used across the app: `toast.success(msg)`,
 * `toast.error(msg, { description })`, `toast.message(msg)`, plus `dismiss`. */
export const toast = Object.assign(
  (title: ReactNode, opts?: ExternalToast) => show("message", title, opts),
  {
    success: (title: ReactNode, opts?: ExternalToast) => show("success", title, opts),
    error: (title: ReactNode, opts?: ExternalToast) => show("error", title, opts),
    message: (title: ReactNode, opts?: ExternalToast) => show("message", title, opts),
    loading: (title: ReactNode, opts?: ExternalToast) => show("message", title, opts),
    custom: sonner.custom,
    dismiss: sonner.dismiss,
    promise: sonner.promise.bind(sonner),
  },
);
