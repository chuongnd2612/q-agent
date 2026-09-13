import { ArrowLeft } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { ProjectMeta } from "./types";
import { Skeleton } from "@/components/ui/Skeleton";

/** Project detail header: back link, provider glyph, name/provider line, and the
 * aggregate knowledge-status pill.
 *
 * While `loading`, the name, the provider line and the glyph are placeholders:
 * before #750 they rendered the route key (a raw GUID) and a default `ado`
 * glyph, which looked like real — and wrong — data rather than a load. The
 * placeholders sit in fixed-height boxes so nothing below them moves when the
 * name lands. */
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
  loading = false,
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
  /** True while the project list is still in flight (see `metaLoading`). */
  loading?: boolean;
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
          {loading ? (
            <div className="h-[46px] w-[46px] shrink-0">
              <Skeleton className="h-full w-full" />
            </div>
          ) : (
            <div
              className="flex h-[46px] w-[46px] shrink-0 items-center justify-center rounded-[13px] text-[18px] font-black"
              style={{ background: glyphBg, color: glyphColor }}
            >
              {glyph}
            </div>
          )}
          <div className="min-w-0 flex-1">
            {loading ? (
              <div data-testid="project-header-skeleton">
                <div className="h-[26px] md:h-[31px]">
                  <Skeleton className="h-full w-[min(260px,70%)]" />
                </div>
                <div className="mt-1.5 h-[15px]">
                  <Skeleton className="h-full w-[min(150px,45%)]" style={{ animationDelay: "90ms" }} />
                </div>
              </div>
            ) : (
              <>
                <h1 className="m-0 text-[22px] font-black tracking-tight md:text-[26px]">
                  {meta.name}
                </h1>
                <div className="truncate font-mono text-[12.5px] text-ink-dim">
                  {meta.repo ? `${meta.repo} · ` : ""}
                  {meta.provider}
                </div>
              </>
            )}
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
