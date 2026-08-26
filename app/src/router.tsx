import { createBrowserRouter, Navigate } from "react-router-dom";
import App from "@/App";
import { RunLayout } from "@/screens/RunLayout";
import { RequireAuth } from "@/screens/RequireAuth";
import { RedirectIfAuthed } from "@/screens/RedirectIfAuthed";

import { Login } from "@/screens/auth/Login";
import { ForgotPassword } from "@/screens/auth/ForgotPassword";
import { SignedOut } from "@/screens/auth/SignedOut";
import { SsoCallback } from "@/screens/auth/SsoCallback";
import { HubSsoEntry } from "@/screens/auth/HubSsoEntry";
import { Profile } from "@/screens/auth/Profile";
import { UserManagement } from "@/screens/settings/UserManagement";
import { ClaudeCredentials } from "@/screens/settings/ClaudeCredentials";
import { SharedWorkspace } from "@/screens/settings/SharedWorkspace";
import { SharedProjectSettings } from "@/screens/settings/SharedProjectSettings";

import { Dashboard } from "@/screens/Dashboard";
import {
  LegacyListRedirect,
  LegacyRunRedirect,
} from "@/screens/LegacyRedirects";
import { GettingStarted } from "@/screens/GettingStarted";
import { Projects } from "@/screens/Projects";
import {
  ProjectConnectionTab,
  ProjectDetail,
  ProjectKnowledgeTab,
  ProjectOverviewTab,
  ProjectTabIndex,
} from "@/screens/ProjectDetail";
import { Tickets } from "@/screens/Tickets";
import { TicketDetail } from "@/screens/TicketDetail";
import { Runs } from "@/screens/Runs";
import { RunDetail } from "@/screens/RunDetail";
import { ReviewCenter } from "@/screens/ReviewCenter";
import { CreateLinkSync } from "@/screens/CreateLinkSync";
import { Automation } from "@/screens/Automation";
import { Execution } from "@/screens/Execution";
import { Evidence } from "@/screens/Evidence";
import { CommentPublish } from "@/screens/CommentPublish";
import { Reports } from "@/screens/Reports";
import { AuditLog } from "@/screens/AuditLog";
import { Settings } from "@/screens/Settings";
import { LocalAgent } from "@/screens/LocalAgent";

/**
 * The route tree from ADR 0003 + auth (ADR 0007). PUBLIC auth screens
 * (`/login`, `/forgot`, `/signed-out`) are top-level siblings of `<App/>`, so
 * they render WITHOUT the app shell. The sign-in screens (`/login`, `/forgot`)
 * are wrapped in `RedirectIfAuthed` so an already-authenticated visitor is
 * bounced to the app; `/signed-out` is intentionally left ungated (logout lands
 * there while still authed). The entire authenticated app is gated by
 * `RequireAuth`, which restores the session (via the refresh cookie) before
 * mounting `<App/>` (providers + shell + <Outlet/>). Run-scoped routes nest
 * under `RunLayout`, which owns the single run WebSocket.
 *
 * The `basename` comes from Vite's `base` (see `vite.config.ts`): '/' when this
 * app owns its hostname, '/qagent/' when it is mounted behind the suite's shared
 * front door. Setting it here is what keeps every `navigate("/runs")` and
 * `to="/settings"` in the app written as though the app owned the root — React
 * Router prepends the basename on the way out and strips it on the way in, so
 * no screen has to know where it is deployed.
 *
 * The exception is anything that bypasses the router — `window.location.*` and
 * plain `<a href>` — which must use `withBase()` from `lib/basePath.ts`.
 */
const ROUTES = [
  // Public sign-in screens — no app shell; authed visitors get bounced to `/`.
  {
    element: <RedirectIfAuthed />,
    children: [
      // `/login` additionally passes through `HubSsoEntry`, which — with the
      // EmeHub integration on (#480) — bounces an anonymous visitor to
      // `/sso/callback` exactly once before letting the local form render.
      { element: <HubSsoEntry />, children: [{ path: "login", element: <Login /> }] },
      { path: "forgot", element: <ForgotPassword /> },
    ],
  },
  // Post-logout confirmation — ungated (logout lands here while still authed).
  { path: "signed-out", element: <SignedOut /> },
  // EmeHub SSO bootstrap (#480) — ungated top-level sibling, like `signed-out`.
  // NOT under `RedirectIfAuthed` (it would bounce a returning user mid-bootstrap)
  // and NOT under `RequireAuth` (arriving anonymous is the entire point).
  { path: "sso/callback", element: <SsoCallback /> },

  // Authenticated app subtree — RequireAuth gates every route below.
  {
    element: <RequireAuth />,
    children: [
      {
        element: <App />,
        children: [
          { index: true, element: <Dashboard /> },
          { path: "getting-started", element: <GettingStarted /> },
          { path: "projects", element: <Projects /> },
          // Addressed by GUID (#585/#587), not by name: names collide across
          // users (#583) and change on rename. An older name-based deep link
          // still lands here and resolves — `ProjectDetail` rewrites it to the
          // canonical GUID URL rather than 404ing.
          //
          // The project is the CONTAINER (ADR 0015): its six tabs are path
          // segments below it, and the tickets/runs/reports lists are the
          // project's own rows rather than the global lists they used to
          // navigate out to (#693).
          {
            path: "projects/:projectGuid",
            element: <ProjectDetail />,
            children: [
              { index: true, element: <ProjectTabIndex /> },
              { path: "overview", element: <ProjectOverviewTab /> },
              { path: "tickets", element: <Tickets /> },
              { path: "tickets/:externalId", element: <TicketDetail /> },
              { path: "runs", element: <Runs /> },
              { path: "knowledge", element: <ProjectKnowledgeTab /> },
              { path: "connection", element: <ProjectConnectionTab /> },
              { path: "reports", element: <Reports /> },
            ],
          },
          // Run stages are a SIBLING of the project layout, not a child of it:
          // they take the whole screen rather than rendering inside the tab
          // body. React Router ranks by specificity, not declaration order, so
          // this wins over the `runs` tab above for a URL that carries a run id.
          // Slice 4 turns it into the full-screen overlay.
          {
            path: "projects/:projectGuid/runs/:runId",
            element: <RunLayout />,
            children: [
              { index: true, element: <RunDetail /> },
              { path: "review", element: <ReviewCenter /> },
              { path: "sync", element: <CreateLinkSync /> },
              { path: "automation", element: <Automation /> },
              { path: "execution", element: <Execution /> },
              { path: "evidence", element: <Evidence /> },
              { path: "comment", element: <CommentPublish /> },
            ],
          },

          // --- pre-#728 flat routes, kept resolvable -----------------------
          // A flat list has no project in the URL and there is deliberately no
          // "current project" to default to, so it lands on the projects list.
          // A flat RUN url can be resolved exactly, because the run now knows
          // its own project (#727).
          { path: "tickets", element: <LegacyListRedirect /> },
          { path: "runs", element: <LegacyListRedirect /> },
          { path: "reports", element: <LegacyListRedirect /> },
          { path: "runs/:runId", element: <LegacyRunRedirect /> },
          { path: "runs/:runId/*", element: <LegacyRunRedirect /> },
          // Ticket detail is not project-shaped beyond its breadcrumb, so the
          // flat form stays a real route: it is the one deep link that can still
          // be honoured without inventing a project for it.
          { path: "tickets/:externalId", element: <TicketDetail /> },
          { path: "audit", element: <AuditLog /> },
          { path: "settings", element: <Settings /> },
          { path: "local-agent", element: <LocalAgent /> },
          { path: "settings/users", element: <UserManagement /> },
          { path: "settings/claude-credentials", element: <ClaudeCredentials /> },
          { path: "settings/shared-workspace", element: <SharedWorkspace /> },
          { path: "settings/shared-workspace/:key", element: <SharedProjectSettings /> },
          { path: "profile", element: <Profile /> },
          { path: "*", element: <Navigate to="/" replace /> },
        ],
      },
    ],
  },
];

export const router = createBrowserRouter(ROUTES, {
  basename: import.meta.env.BASE_URL,
});
