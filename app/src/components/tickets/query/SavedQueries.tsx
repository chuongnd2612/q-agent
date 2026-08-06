/**
 * Saved queries — the derived presets and the user's own, above the clause rows.
 *
 * Ported from EmeHub's `components/query/SavedQueries.tsx`. One list, both kinds,
 * told apart by a `Preset` pill: they do the same job, and splitting them into
 * two lists would make the presets feel like documentation rather than something
 * to click.
 *
 * Loading a query only fills the **draft** — it does not run. Same rule as the
 * rest of the builder: nothing queries until Apply.
 *
 * A preset offers no Delete, because there is nothing to delete: it is derived
 * from the caller's own filter options every render (`savedQueries.presetsFor`),
 * so it reappears the moment the data still supports it.
 */

import { X } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { TicketQuery } from "./model";
import type { Preset, SavedQuery } from "./queryStore";

export interface SavedQueriesProps {
  presets: Preset[];
  saved: SavedQuery[];
  /** The query to store when the user names one. */
  draft: TicketQuery;
  /** Fills the draft. Never runs it — Apply does that. */
  onLoad: (query: TicketQuery) => void;
  onSave: (name: string) => void;
  onDelete: (id: string) => void;
  /** False while the draft is invalid, so an unrunnable query cannot be saved. */
  canSave: boolean;
}

export function SavedQueries({
  presets,
  saved,
  draft,
  onLoad,
  onSave,
  onDelete,
  canSave,
}: SavedQueriesProps) {
  const { t } = useTranslation("tickets");
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");

  const commit = () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    onSave(trimmed);
    setName("");
    setNaming(false);
  };

  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex items-center gap-3">
        <span className="text-[10.5px] font-bold uppercase tracking-[.11em] text-ink-dim">
          {t("builder.saved")}
        </span>
        {!naming && (
          <button
            type="button"
            onClick={() => setNaming(true)}
            disabled={!canSave}
            title={canSave ? undefined : t("builder.finishFirst")}
            className="ml-auto cursor-pointer rounded-[10px] border border-white/[0.09] bg-white/[0.05] px-2.5 py-1 text-[11.5px] font-semibold text-[#dcdce4] transition-colors hover:bg-white/[0.1] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {t("builder.saveThis")}
          </button>
        )}
      </div>

      {naming && (
        <div className="flex items-center gap-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                commit();
              }
              if (e.key === "Escape") setNaming(false);
            }}
            placeholder={t("builder.nameThis")}
            aria-label={t("builder.queryName")}
            autoFocus
            className="h-8 max-w-[260px] flex-1 rounded-[10px] border border-white/[0.09] bg-white/[0.05] px-3 text-[12.5px] text-ink outline-none placeholder:text-ink-dim focus:border-[rgba(139,92,246,.45)]"
          />
          <button
            type="button"
            onClick={commit}
            disabled={!name.trim()}
            className="cursor-pointer rounded-[10px] border border-transparent bg-[rgba(139,92,246,.9)] px-3 py-[6px] text-[11.5px] font-bold text-white disabled:cursor-not-allowed disabled:opacity-50"
          >
            {t("builder.save")}
          </button>
          <button
            type="button"
            onClick={() => setNaming(false)}
            className="cursor-pointer bg-transparent p-1 text-[11.5px] font-semibold text-ink-dim hover:text-ink"
          >
            {t("builder.cancel")}
          </button>
        </div>
      )}

      {presets.length === 0 && saved.length === 0 ? (
        <p className="m-0 text-[11.5px] text-ink-dim">{t("builder.nothingSaved")}</p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {presets.map((preset) => (
            <span
              key={preset.key}
              className="flex items-center gap-1.5 rounded-[10px] border border-white/[0.09] bg-white/[0.05] py-1 pl-2.5 pr-2 transition-colors hover:border-[rgba(139,92,246,.45)]"
            >
              <button
                type="button"
                onClick={() => onLoad(preset.query)}
                className="cursor-pointer bg-transparent p-0 text-[12px] font-semibold text-ink"
              >
                {t(`builder.presets.${preset.key}`)}
              </button>
              <span className="rounded-[6px] bg-white/[0.08] px-1.5 py-0.5 text-[9.5px] font-bold uppercase tracking-wide text-ink-dim">
                {t("builder.preset")}
              </span>
            </span>
          ))}
          {saved.map((query) => (
            <span
              key={query.id}
              className="flex items-center gap-1 rounded-[10px] border border-white/[0.09] bg-white/[0.05] py-1 pl-2.5 pr-1 transition-colors hover:border-[rgba(139,92,246,.45)]"
            >
              <button
                type="button"
                onClick={() => onLoad(query.query)}
                className="cursor-pointer bg-transparent p-0 text-[12px] font-semibold text-ink"
              >
                {query.name}
              </button>
              <button
                type="button"
                onClick={() => onDelete(query.id)}
                aria-label={t("builder.deleteQuery", { name: query.name })}
                className="cursor-pointer bg-transparent p-1 text-ink-dim hover:text-red-400"
              >
                <X size={12} strokeWidth={2.4} />
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
