import { Check, Pencil, Pin, Plus, X } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/Button";
import { useEditRepoKnowledge } from "@/hooks/queries";
import { toast } from "@/lib/toast";
import type { KnowledgeBody, KnowledgeRoute, KnowledgeSelector } from "@/types/api";

/** A route/selector entry as stored, plus the stamp a manual edit carries (#828). */
type Stamped = { origin?: string; pinned?: boolean };
type StoredRoute = KnowledgeRoute & Stamped & { auth_required?: boolean };
type StoredSelector = KnowledgeSelector & Stamped;

/** Which entry the panel currently has open for editing. `index: -1` is a new one. */
type Editing = { section: "routes" | "selectors"; index: number } | null;

const FIELD =
  "w-full rounded-[10px] border border-bd2 bg-field px-3 py-2 text-[12.5px] text-txt outline-none focus:border-p";

/**
 * The code knowledge base's **edit affordance** (#828, ADR 0016 §5).
 *
 * Until this existed the KB was rebuild-only: a wrong selector could only be
 * fixed by re-bootstrapping and hoping. Every entry saved here is stamped
 * `{origin: "manual", pinned: true}` by the API, which is what makes the
 * correction survive a self-heal and a later rebuild (#827) — so the panel
 * badges pinned entries rather than leaving that invisible.
 *
 * Opaque surface (`bg-pop`, no `backdrop-filter`): it layers over the animated
 * shell, where a translucent panel is both a compositing artifact and a
 * stacking-context trap, and every colour is a token so it reads in light mode
 * as well as dark (#844).
 *
 * @param projectKey Project the repo belongs to (GUID or name — the API resolves both).
 * @param repo Repository whose knowledge base is being corrected.
 * @param knowledge The current blob, read to seed each entry's form.
 * @param onClose Closes the panel.
 */
export function KnowledgeEditPanel({
  projectKey,
  repo,
  knowledge,
  onClose,
}: {
  projectKey: string;
  repo: string;
  knowledge: Partial<KnowledgeBody>;
  onClose: () => void;
}) {
  const { t } = useTranslation("projects");
  const edit = useEditRepoKnowledge(projectKey);
  const routes = (knowledge.routes ?? []) as StoredRoute[];
  const selectors = (knowledge.selectors ?? []) as StoredSelector[];

  const [editing, setEditing] = useState<Editing>(null);
  const [draft, setDraft] = useState<Record<string, string | boolean>>({});
  const [domain, setDomain] = useState(knowledge.domain ?? "");
  const [entities, setEntities] = useState((knowledge.business_entities ?? []).join(", "));

  const save = (body: Parameters<typeof edit.mutateAsync>[0]["body"]) =>
    edit
      .mutateAsync({ repo, body })
      .then(() => {
        toast.success(t("repoKnowledge.edit.saved"));
        setEditing(null);
      })
      .catch((e: Error) => toast.error(e.message || t("repoKnowledge.edit.failed")));

  const openEntry = (section: "routes" | "selectors", index: number) => {
    const entry =
      index < 0
        ? {}
        : section === "routes"
          ? {
              path: routes[index].path ?? "",
              description: routes[index].description ?? "",
              authRequired: Boolean(routes[index].auth_required ?? routes[index].authRequired),
              // The identity being corrected — see `replaces` in KnowledgePatchRequest.
              replaces: routes[index].path ?? "",
            }
          : {
              screen: selectors[index].screen ?? "",
              element: selectors[index].element ?? "",
              selector: selectors[index].selector ?? "",
              replaces: selectors[index].selector ?? "",
            };
    setDraft(entry as Record<string, string | boolean>);
    setEditing({ section, index });
  };

  /** Save the open draft as one route/selector. `replaces` carries the identity
   *  of the entry being corrected, so fixing a wrong value replaces it instead
   *  of adding a second, contradicting entry beside it. */
  const saveRoute = () =>
    save({
      routes: [
        {
          path: String(draft.path ?? ""),
          description: String(draft.description ?? ""),
          authRequired: Boolean(draft.authRequired),
          replaces: String(draft.replaces ?? ""),
        },
      ],
    });
  const saveSelector = () =>
    save({
      selectors: [
        {
          screen: String(draft.screen ?? ""),
          element: String(draft.element ?? ""),
          selector: String(draft.selector ?? ""),
          replaces: String(draft.replaces ?? ""),
        },
      ],
    });

  const isOpen = (section: "routes" | "selectors", index: number) =>
    editing?.section === section && editing.index === index;

  const stamp = (entry: Stamped) =>
    entry.pinned ? (
      <span
        className="inline-flex items-center gap-1 rounded-md bg-p/15 px-1.5 py-0.5 text-[10px] font-bold text-brand-soft"
        title={t("repoKnowledge.edit.pinnedHint")}
      >
        <Pin size={9} strokeWidth={2.6} /> {t("repoKnowledge.edit.pinned")}
      </span>
    ) : null;

  return (
    <div className="mb-3.5 overflow-hidden rounded-2xl border border-bd2 bg-pop">
      <div className="flex flex-wrap items-center gap-2.5 border-b border-bd3 px-[18px] py-3">
        <span className="text-[13.5px] font-bold text-txt">{t("repoKnowledge.edit.title")}</span>
        <span className="flex-1 text-[12px] text-txt4">{t("repoKnowledge.edit.subtitle")}</span>
        <Button variant="ghost" size="sm" onClick={onClose} aria-label={t("repoKnowledge.edit.close")}>
          <X size={14} strokeWidth={2.3} /> {t("repoKnowledge.edit.close")}
        </Button>
      </div>

      <div className="flex flex-col gap-5 px-[18px] py-4">
        {/* ------------------------------------------------ business context */}
        <section className="flex flex-col gap-2">
          <label className="text-[11px] font-bold uppercase tracking-[0.08em] text-label">
            {t("repoKnowledge.sectionDomain")}
          </label>
          <textarea
            className={`${FIELD} min-h-[72px] resize-y leading-relaxed`}
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            placeholder={t("repoKnowledge.edit.domainPlaceholder")}
          />
          <label className="mt-1 text-[11px] font-bold uppercase tracking-[0.08em] text-label">
            {t("repoKnowledge.edit.businessEntities")}
          </label>
          <input
            className={FIELD}
            value={entities}
            onChange={(e) => setEntities(e.target.value)}
            placeholder={t("repoKnowledge.edit.entitiesPlaceholder")}
          />
          <div className="flex items-center gap-2">
            <Button
              variant="primary"
              size="sm"
              disabled={edit.isPending}
              onClick={() =>
                save({
                  domain,
                  businessEntities: entities
                    .split(",")
                    .map((s) => s.trim())
                    .filter(Boolean),
                })
              }
            >
              <Check size={14} strokeWidth={2.4} /> {t("repoKnowledge.edit.saveContext")}
            </Button>
            <span className="text-[11.5px] text-txt4">{t("repoKnowledge.edit.contextHint")}</span>
          </div>
        </section>

        {/* -------------------------------------------------------- routes */}
        <section className="flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <span className="text-[11px] font-bold uppercase tracking-[0.08em] text-label">
              {t("repoKnowledge.edit.routes")}
            </span>
            <Button variant="ghost" size="sm" onClick={() => openEntry("routes", -1)}>
              <Plus size={13} strokeWidth={2.5} /> {t("repoKnowledge.edit.addRoute")}
            </Button>
          </div>
          {isOpen("routes", -1) && (
            <RouteForm
              draft={draft}
              setDraft={setDraft}
              pending={edit.isPending}
              t={t}
              onCancel={() => setEditing(null)}
              onSave={saveRoute}
            />
          )}
          {routes.length === 0 && !isOpen("routes", -1) && (
            <p className="m-0 text-[12.5px] text-txt4">{t("repoKnowledge.edit.noRoutes")}</p>
          )}
          {routes.map((route, i) =>
            isOpen("routes", i) ? (
              <RouteForm
                key={route.path || i}
                draft={draft}
                setDraft={setDraft}
                pending={edit.isPending}
                t={t}
                onCancel={() => setEditing(null)}
                onSave={saveRoute}
              />
            ) : (
              <div
                key={route.path || i}
                className="flex flex-wrap items-center gap-2 rounded-[10px] border border-bd3 bg-inset px-3 py-2"
              >
                <span className="font-mono text-[12px] font-semibold text-txt2">{route.path}</span>
                <span className="min-w-[80px] flex-1 truncate text-[12px] text-txt4">
                  {route.description}
                </span>
                {stamp(route)}
                <Button variant="ghost" size="sm" onClick={() => openEntry("routes", i)}>
                  <Pencil size={13} strokeWidth={2.3} /> {t("repoKnowledge.edit.edit")}
                </Button>
              </div>
            ),
          )}
        </section>

        {/* ----------------------------------------------------- selectors */}
        <section className="flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <span className="text-[11px] font-bold uppercase tracking-[0.08em] text-label">
              {t("repoKnowledge.edit.selectors")}
            </span>
            <Button variant="ghost" size="sm" onClick={() => openEntry("selectors", -1)}>
              <Plus size={13} strokeWidth={2.5} /> {t("repoKnowledge.edit.addSelector")}
            </Button>
          </div>
          {isOpen("selectors", -1) && (
            <SelectorForm
              draft={draft}
              setDraft={setDraft}
              pending={edit.isPending}
              t={t}
              onCancel={() => setEditing(null)}
              onSave={saveSelector}
            />
          )}
          {selectors.length === 0 && !isOpen("selectors", -1) && (
            <p className="m-0 text-[12.5px] text-txt4">{t("repoKnowledge.edit.noSelectors")}</p>
          )}
          {selectors.map((sel, i) =>
            isOpen("selectors", i) ? (
              <SelectorForm
                key={sel.selector || i}
                draft={draft}
                setDraft={setDraft}
                pending={edit.isPending}
                t={t}
                onCancel={() => setEditing(null)}
                onSave={saveSelector}
              />
            ) : (
              <div
                key={sel.selector || i}
                className="flex flex-wrap items-center gap-2 rounded-[10px] border border-bd3 bg-inset px-3 py-2"
              >
                <span className="text-[12px] font-semibold text-txt2">
                  {sel.screen} · {sel.element}
                </span>
                <span className="min-w-[80px] flex-1 truncate font-mono text-[12px] text-txt4">
                  {sel.selector}
                </span>
                {stamp(sel)}
                <Button variant="ghost" size="sm" onClick={() => openEntry("selectors", i)}>
                  <Pencil size={13} strokeWidth={2.3} /> {t("repoKnowledge.edit.edit")}
                </Button>
              </div>
            ),
          )}
        </section>
      </div>
    </div>
  );
}

type FormProps = {
  draft: Record<string, string | boolean>;
  setDraft: (d: Record<string, string | boolean>) => void;
  pending: boolean;
  t: (k: string) => string;
  onSave: () => void;
  onCancel: () => void;
};

/** Inline editor for one route. `path` is the entry's identity — editing it
 *  upserts a different entry rather than renaming this one. */
function RouteForm({ draft, setDraft, pending, t, onSave, onCancel }: FormProps) {
  return (
    <div className="flex flex-col gap-2 rounded-[10px] border border-p/35 bg-inset px-3 py-3">
      <input
        className={`${FIELD} font-mono`}
        value={String(draft.path ?? "")}
        onChange={(e) => setDraft({ ...draft, path: e.target.value })}
        placeholder={t("repoKnowledge.edit.pathPlaceholder")}
      />
      <input
        className={FIELD}
        value={String(draft.description ?? "")}
        onChange={(e) => setDraft({ ...draft, description: e.target.value })}
        placeholder={t("repoKnowledge.edit.descriptionPlaceholder")}
      />
      <label className="flex items-center gap-2 text-[12.5px] text-txt3">
        <input
          type="checkbox"
          checked={Boolean(draft.authRequired)}
          onChange={(e) => setDraft({ ...draft, authRequired: e.target.checked })}
        />
        {t("repoKnowledge.edit.authRequired")}
      </label>
      <FormActions pending={pending} t={t} onSave={onSave} onCancel={onCancel} />
    </div>
  );
}

/** Inline editor for one selector. `selector` is the entry's identity. */
function SelectorForm({ draft, setDraft, pending, t, onSave, onCancel }: FormProps) {
  return (
    <div className="flex flex-col gap-2 rounded-[10px] border border-p/35 bg-inset px-3 py-3">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <input
          className={FIELD}
          value={String(draft.screen ?? "")}
          onChange={(e) => setDraft({ ...draft, screen: e.target.value })}
          placeholder={t("repoKnowledge.edit.screenPlaceholder")}
        />
        <input
          className={FIELD}
          value={String(draft.element ?? "")}
          onChange={(e) => setDraft({ ...draft, element: e.target.value })}
          placeholder={t("repoKnowledge.edit.elementPlaceholder")}
        />
      </div>
      <input
        className={`${FIELD} font-mono`}
        value={String(draft.selector ?? "")}
        onChange={(e) => setDraft({ ...draft, selector: e.target.value })}
        placeholder={t("repoKnowledge.edit.selectorPlaceholder")}
      />
      <FormActions pending={pending} t={t} onSave={onSave} onCancel={onCancel} />
    </div>
  );
}

function FormActions({
  pending,
  t,
  onSave,
  onCancel,
}: Pick<FormProps, "pending" | "t" | "onSave" | "onCancel">) {
  return (
    <div className="flex items-center gap-2">
      <Button variant="primary" size="sm" disabled={pending} onClick={onSave}>
        <Check size={14} strokeWidth={2.4} /> {t("repoKnowledge.edit.save")}
      </Button>
      <Button variant="ghost" size="sm" onClick={onCancel}>
        {t("repoKnowledge.edit.cancel")}
      </Button>
    </div>
  );
}
