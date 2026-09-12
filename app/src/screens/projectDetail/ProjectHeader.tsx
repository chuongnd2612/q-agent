import { ArrowLeft } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { ProjectMeta } from "./types";

/** Project detail header: back link, provider glyph, name/provider line, and the
 * aggregate knowledge-status pill. */
export function ProjectHeader({
  meta,
  glyph,
  glyphBg,
  glyphColor,
  statusBg,
  statusDot,
  statusColor,
  statusLabel,
  onBack,
}: {
  meta: ProjectMeta;
  glyph: string;
  glyphBg: string;
  glyphColor: string;
  statusBg: string;
  statusDot: string;
  statusColor: string;
  statusLabel: string;
  onBack: () => void;
}) {
  const { t } = useTranslation("projects");
  return (
    <>
      <button
        onClick={onBack}
        className="mb-3.5 flex cursor-pointer items-center gap-[7px] border-none bg-transparent p-0 text-[12.5px] font-semibold text-ink-dim hover:text-txt"
      >
        <ArrowLeft size={14} strokeWidth={2.2} /> {t("header.back")}
      </button>

      <div className="mb-4 flex flex-col gap-3.5 md:flex-row md:items-center">
        <div className="flex min-w-0 flex-1 items-center gap-3.5">
          <div
            className="flex h-[46px] w-[46px] shrink-0 items-center justify-center rounded-[13px] text-[18px] font-black"
            style={{ background: glyphBg, color: glyphColor }}
          >
            {glyph}
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="m-0 text-[22px] font-black tracking-tight md:text-[26px]">{meta.name}</h1>
            <div className="truncate font-mono text-[12.5px] text-ink-dim">
              {meta.repo ? `${meta.repo} · ` : ""}
              {meta.provider}
            </div>
          </div>
        </div>
        <div
          className="flex items-center gap-2 self-start rounded-xl px-3 py-2 md:self-auto"
          style={{ background: statusBg }}
        >
          <span className="h-2 w-2 rounded-full" style={{ background: statusDot }} />
          <span className="text-[12.5px] font-bold" style={{ color: statusColor }}>
            {t("header.knowledge", { status: statusLabel })}
          </span>
        </div>
      </div>
    </>
  );
}
