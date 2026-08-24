import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { toast } from "@/lib/toast";
import { useReadiness } from "@/hooks/queries";
import { blockerFor, fixRoute } from "./setupItems";

/**
 * Pre-flight check for an action with known prerequisites (#643).
 *
 * `guard(keys, run)` runs the action, or — when one of `keys` is a real blocker —
 * refuses and raises a toast naming the missing thing with a button that goes to
 * the fix. Refusing is the point: the alternative was firing the request, getting
 * a 409 the user may never see, and leaving a spinner spinning.
 *
 * It only refuses on a blocker it can actually see. If readiness hasn't loaded
 * (or the endpoint is unreachable) the action proceeds — a client-side check that
 * can silently disable the product when a side query fails is worse than the
 * failure it prevents, and the server still enforces the real rule.
 */
export function useSetupGuard() {
  const { t } = useTranslation("common");
  const navigate = useNavigate();
  const { data: readiness } = useReadiness();

  return (keys: string[], run: () => void) => {
    for (const key of keys) {
      const blocker = readiness ? blockerFor(readiness.items, key) : null;
      if (!blocker) continue;
      const route = fixRoute(blocker.fix);
      toast.error(t(`setup.items.${blocker.key}.title`), {
        description: blocker.detail || t(`setup.items.${blocker.key}.why`),
        duration: 8000,
        ...(route
          ? { action: { label: t(`setup.fix.${blocker.fix}`), onClick: () => navigate(route) } }
          : {}),
      });
      return;
    }
    run();
  };
}
