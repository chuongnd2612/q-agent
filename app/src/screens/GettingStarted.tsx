/**
 * Getting Started / User Guide — a comprehensive in-app manual for Q-Agent.
 *
 * A single scrollable page of GlassCard sections that explains what Q-Agent is,
 * its core concepts, the 8-stage run pipeline (mirroring RunSidebar's PIPELINE),
 * a first-run walkthrough, provider/credential setup, and a short FAQ. The hero
 * offers two CTAs: launch the interactive product tour, or seed + open a live
 * sample run to explore hands-on.
 */
import type { ComponentType } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import {
  Rocket,
  Compass,
  BookOpen,
  FolderGit2,
  Ticket,
  SquareStack,
  Library,
  ListChecks,
  Sparkles,
  ClipboardCheck,
  GitBranch,
  FlaskConical,
  PlayCircle,
  Camera,
  MessageSquare,
  Loader2,
  PlugZap,
  KeyRound,
  HelpCircle,
} from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import { Button } from "@/components/ui/Button";
import { useTour } from "@/store/tour";
import { useEnsureSampleRun } from "@/hooks/queries";
import { SetupChecklist } from "@/components/setup/SetupChecklist";

/** A single core concept, rendered as a card in the concepts grid.
 * `key` indexes into the `gettingStarted.concepts.*` i18n subtree. */
interface Concept {
  icon: ComponentType<{ size?: number; strokeWidth?: number }>;
  key: string;
}

const CONCEPTS: Concept[] = [
  { icon: FolderGit2, key: "projects" },
  { icon: Ticket, key: "tickets" },
  { icon: SquareStack, key: "runs" },
  { icon: Library, key: "knowledgeBases" },
];

/** One stage of the run pipeline — mirrors RunSidebar's PIPELINE order/labels.
 * `key` indexes into the `gettingStarted.pipeline.*` i18n subtree. */
interface Stage {
  icon: ComponentType<{ size?: number; strokeWidth?: number; className?: string }>;
  key: string;
}

const PIPELINE: Stage[] = [
  { icon: ListChecks, key: "syncSelect" },
  { icon: Sparkles, key: "analyze" },
  { icon: ClipboardCheck, key: "review" },
  { icon: GitBranch, key: "link" },
  { icon: FlaskConical, key: "automation" },
  { icon: PlayCircle, key: "execution" },
  { icon: Camera, key: "evidence" },
  { icon: MessageSquare, key: "publish" },
];

/** A numbered step in the "Your first run" walkthrough — `key` indexes into
 * the `gettingStarted.firstRun.*` i18n subtree. */
const FIRST_RUN: string[] = [
  "connectProvider",
  "syncTickets",
  "selectCreateRun",
  "reviewCases",
  "linkUpstream",
  "generateAutomation",
  "execute",
  "reviewEvidence",
];

/** A single FAQ entry — `key` indexes into the `gettingStarted.faq.*` subtree. */
const FAQ: string[] = ["credentials", "rewatchTour", "runsLive", "editDrafts"];

export function GettingStarted() {
  const { t } = useTranslation("dashboard");
  const navigate = useNavigate();
  const startTour = useTour((s) => s.start);
  const ensureSample = useEnsureSampleRun();

  const openSampleRun = () => {
    ensureSample.mutate(undefined, {
      onSuccess: (run) => navigate(`/runs/${run.id}`),
    });
  };

  return (
    <div className="px-1 pb-10 pt-0.5">
      {/* Hero */}
      <div className="mb-[22px]">
        <div className="mb-[5px] text-[13px] font-medium text-muted">{t("gettingStarted.hero.eyebrow")}</div>
        <h1 className="m-0 text-[28px] font-black tracking-tight">{t("gettingStarted.hero.title")}</h1>
        <p className="mb-0 mt-2.5 max-w-[640px] text-[13.5px] leading-relaxed text-ink-dim">
          {t("gettingStarted.hero.subtitle")}
        </p>
        <div className="mt-4 flex flex-wrap gap-2.5">
          <Button variant="primary" onClick={startTour}>
            <Compass size={15} /> {t("gettingStarted.hero.takeTour")}
          </Button>
          <Button variant="glass" onClick={openSampleRun} disabled={ensureSample.isPending}>
            {ensureSample.isPending ? (
              <Loader2 size={15} className="animate-spin" />
            ) : (
              <Rocket size={15} />
            )}
            {ensureSample.isPending ? t("gettingStarted.hero.preparingSample") : t("gettingStarted.hero.exploreSample")}
          </Button>
        </div>
      </div>

      {/* Live setup state, above the prose (#643): a new account's first question
          is "what do I still need?", and the guide below can only answer it in
          general terms. */}
      <div className="mb-3.5">
        <SetupChecklist />
      </div>

      {/* What is Q-Agent */}
      <GlassCard index={0} className="mb-3.5 p-6">
        <div className="mb-3 flex items-center gap-2.5">
          <span
            className="flex h-9 w-9 items-center justify-center rounded-[11px]"
            style={{ background: "linear-gradient(135deg,rgba(139,92,246,.22),rgba(34,211,238,.12))", color: "#c4b5fd" }}
          >
            <BookOpen size={18} strokeWidth={2} />
          </span>
          <h2 className="m-0 text-[17px] font-extrabold tracking-tight">{t("gettingStarted.whatIs.title")}</h2>
        </div>
        <p className="m-0 max-w-[760px] text-[13.5px] leading-relaxed text-ink-soft">
          {t("gettingStarted.whatIs.body1")}
        </p>
        <p className="m-0 mt-3 max-w-[760px] text-[13.5px] leading-relaxed text-ink-dim">
          {t("gettingStarted.whatIs.body2")}
        </p>
      </GlassCard>

      {/* Core concepts */}
      <div className="mb-1.5 mt-6 px-1 text-[11px] font-semibold tracking-[0.12em] text-muted">
        {t("gettingStarted.sections.coreConcepts")}
      </div>
      <div className="mb-3.5 grid grid-cols-2 gap-3.5 lg:grid-cols-4">
        {CONCEPTS.map((c, i) => {
          const Icon = c.icon;
          return (
            <GlassCard key={c.key} index={i} hover className="p-[18px]">
              <span
                className="mb-3 flex h-9 w-9 items-center justify-center rounded-[11px]"
                style={{ background: "rgba(139,92,246,.14)", color: "#c4b5fd" }}
              >
                <Icon size={17} strokeWidth={2} />
              </span>
              <div className="mb-1.5 text-[14px] font-bold tracking-tight">{t(`gettingStarted.concepts.${c.key}.title`)}</div>
              <p className="m-0 text-[12.5px] leading-relaxed text-ink-dim">{t(`gettingStarted.concepts.${c.key}.body`)}</p>
            </GlassCard>
          );
        })}
      </div>

      {/* The run pipeline */}
      <div className="mb-1.5 mt-6 px-1 text-[11px] font-semibold tracking-[0.12em] text-muted">
        {t("gettingStarted.sections.runPipeline")}
      </div>
      <GlassCard index={0} className="mb-3.5 p-6">
        <h2 className="m-0 mb-1 text-[17px] font-extrabold tracking-tight">{t("gettingStarted.pipelineIntro.title")}</h2>
        <p className="m-0 mb-5 max-w-[720px] text-[13px] leading-relaxed text-ink-dim">
          {t("gettingStarted.pipelineIntro.body")}
        </p>
        <div className="relative flex flex-col gap-px">
          {/* connector rail behind the nodes (node center ≈ 18px from the left) */}
          <div className="absolute bottom-6 left-[18px] top-6 w-0.5 bg-white/[0.09]" />
          {PIPELINE.map((stage, i) => {
            const Icon = stage.icon;
            return (
              <div key={stage.key} className="flex items-start gap-[13px] rounded-[11px] px-2 py-2.5">
                <span
                  className="relative z-10 flex h-9 w-9 shrink-0 items-center justify-center rounded-full font-mono text-[11px] font-bold text-white"
                  style={{
                    background: "linear-gradient(135deg,#8b5cf6,#6366f1)",
                    boxShadow: "0 0 0 4px rgba(139,92,246,.14)",
                  }}
                >
                  {i + 1}
                </span>
                <div className="min-w-0 flex-1 pt-0.5">
                  <div className="flex items-center gap-2">
                    <Icon size={15} strokeWidth={2} className="text-violet" />
                    <span className="text-[14px] font-bold tracking-tight">{t(`gettingStarted.pipeline.${stage.key}.label`)}</span>
                  </div>
                  <p className="m-0 mt-1 text-[12.5px] leading-relaxed text-ink-dim">{t(`gettingStarted.pipeline.${stage.key}.what`)}</p>
                </div>
              </div>
            );
          })}
        </div>
      </GlassCard>

      {/* Your first run */}
      <div className="mb-1.5 mt-6 px-1 text-[11px] font-semibold tracking-[0.12em] text-muted">
        {t("gettingStarted.sections.yourFirstRun")}
      </div>
      <GlassCard index={0} className="mb-3.5 p-6">
        <h2 className="m-0 mb-1 text-[17px] font-extrabold tracking-tight">{t("gettingStarted.firstRunIntro.title")}</h2>
        <p className="m-0 mb-5 max-w-[720px] text-[13px] leading-relaxed text-ink-dim">
          {t("gettingStarted.firstRunIntro.body")}
        </p>
        <ol className="m-0 grid list-none grid-cols-1 gap-2.5 p-0 md:grid-cols-2">
          {FIRST_RUN.map((step, i) => (
            <li
              key={step}
              className="flex items-start gap-3 rounded-[13px] p-3"
              style={{ background: "rgba(255,255,255,.03)", border: "1px solid rgba(255,255,255,.05)" }}
            >
              <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full font-mono text-[11px] font-bold text-[#c4b5fd]" style={{ background: "rgba(139,92,246,.16)" }}>
                {i + 1}
              </span>
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-bold tracking-tight">{t(`gettingStarted.firstRun.${step}.title`)}</div>
                <p className="m-0 mt-0.5 text-[12px] leading-relaxed text-ink-dim">{t(`gettingStarted.firstRun.${step}.body`)}</p>
              </div>
            </li>
          ))}
        </ol>
      </GlassCard>

      {/* Providers & Claude credentials */}
      <div className="mb-1.5 mt-6 px-1 text-[11px] font-semibold tracking-[0.12em] text-muted">
        {t("gettingStarted.sections.providersCredentials")}
      </div>
      <div className="mb-3.5 grid grid-cols-1 gap-3.5 md:grid-cols-2">
        <GlassCard index={0} className="p-[22px]">
          <span
            className="mb-3 flex h-9 w-9 items-center justify-center rounded-[11px]"
            style={{ background: "rgba(34,211,238,.13)", color: "#22d3ee" }}
          >
            <PlugZap size={17} strokeWidth={2} />
          </span>
          <div className="mb-1.5 text-[14px] font-bold tracking-tight">{t("gettingStarted.providers.connections.title")}</div>
          <p className="m-0 text-[12.5px] leading-relaxed text-ink-dim">
            {t("gettingStarted.providers.connections.body")}
          </p>
        </GlassCard>
        <GlassCard index={1} className="p-[22px]">
          <span
            className="mb-3 flex h-9 w-9 items-center justify-center rounded-[11px]"
            style={{ background: "rgba(139,92,246,.14)", color: "#c4b5fd" }}
          >
            <KeyRound size={17} strokeWidth={2} />
          </span>
          <div className="mb-1.5 text-[14px] font-bold tracking-tight">{t("gettingStarted.providers.claude.title")}</div>
          <p className="m-0 text-[12.5px] leading-relaxed text-ink-dim">
            {t("gettingStarted.providers.claude.body")}
          </p>
        </GlassCard>
      </div>

      {/* Tips & FAQ */}
      <div className="mb-1.5 mt-6 px-1 text-[11px] font-semibold tracking-[0.12em] text-muted">
        {t("gettingStarted.sections.tipsFaq")}
      </div>
      <GlassCard index={0} className="mb-3.5 p-6">
        <div className="flex flex-col gap-3.5">
          {FAQ.map((item) => (
            <div key={item} className="flex items-start gap-3">
              <span className="mt-0.5 shrink-0 text-violet">
                <HelpCircle size={16} strokeWidth={2} />
              </span>
              <div className="min-w-0 flex-1">
                <div className="text-[13.5px] font-bold tracking-tight">{t(`gettingStarted.faq.${item}.q`)}</div>
                <p className="m-0 mt-0.5 text-[12.5px] leading-relaxed text-ink-dim">{t(`gettingStarted.faq.${item}.a`)}</p>
              </div>
            </div>
          ))}
        </div>
        <div className="mt-5 flex flex-wrap gap-2.5 border-t border-white/[0.06] pt-5">
          <Button variant="primary" onClick={startTour}>
            <Compass size={15} /> {t("gettingStarted.hero.takeTour")}
          </Button>
          <Button variant="glass" onClick={openSampleRun} disabled={ensureSample.isPending}>
            {ensureSample.isPending ? (
              <Loader2 size={15} className="animate-spin" />
            ) : (
              <Rocket size={15} />
            )}
            {ensureSample.isPending ? t("gettingStarted.hero.preparingSample") : t("gettingStarted.hero.exploreSample")}
          </Button>
        </div>
      </GlassCard>
    </div>
  );
}
