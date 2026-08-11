import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router-dom";
import { I18nextProvider } from "react-i18next";
import i18n from "@/i18n";
import { router } from "@/router";
import { installHubSessionAuthority } from "@/app/sessionRenewal";
import "@/index.css";

// Before the first render: makes the hub the authority on who is signed in, so an
// SSO session cannot outlive the hub session that authorised it (#531). A no-op
// on deployments without the integration.
installHubSessionAuthority();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <I18nextProvider i18n={i18n}>
      <RouterProvider router={router} />
    </I18nextProvider>
  </StrictMode>,
);
