import { useEffect } from "react";
import { Toaster } from "sonner";
import { AppBackground } from "@/components/background/AppBackground";
import { AppLayout } from "@/components/shell/AppLayout";
import { QueryProvider } from "@/app/QueryProvider";
import { useUI } from "@/store/ui";

import { KnowledgeBuildOverlay } from "@/screens/KnowledgeBuildOverlay";
import { CommandPalette } from "@/screens/CommandPalette";
import { CreateRunModal } from "@/screens/CreateRunModal";
import { TourOverlay } from "@/components/tour/TourOverlay";
import { ServiceUnreachableBanner } from "@/components/system/ServiceUnreachableBanner";

/**
 * Root layout element for the data router (see router.tsx). Wraps the app in the
 * query provider and background, mounts the shell (`AppLayout` renders the
 * matched route via `<Outlet/>`), and global overlays.
 */
export default function App() {
  const togglePalette = useUI((s) => s.togglePalette);
  const closePalette = useUI((s) => s.closePalette);
  const closeCreateRun = useUI((s) => s.closeCreateRun);
  const closeDrawer = useUI((s) => s.closeDrawer);
  const closeChat = useUI((s) => s.closeChat);

  // Global keyboard: ⌘K / Ctrl-K toggles the palette; Escape closes overlays.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        togglePalette();
      }
      if (e.key === "Escape") {
        closePalette();
        closeCreateRun();
        closeDrawer();
        closeChat();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [togglePalette, closePalette, closeCreateRun, closeDrawer, closeChat]);

  return (
    <QueryProvider>
      <AppBackground />
      <AppLayout />
      <CommandPalette />
      <CreateRunModal />
      <KnowledgeBuildOverlay />
      <TourOverlay />
      {/* Inside QueryProvider — Retry refetches the failed queries. Renders
          nothing while the backend is reachable. */}
      <ServiceUnreachableBanner />
      {/* Every toast is rendered by our custom card (see @/lib/toast), so the
          Toaster itself is unstyled — it only provides positioning + lifecycle. */}
      <Toaster position="bottom-center" toastOptions={{ unstyled: true }} />
    </QueryProvider>
  );
}
