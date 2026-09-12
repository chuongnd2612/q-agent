import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, CheckCircle2, Info, Paperclip } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Dropdown";
import {
  useCreateBusinessSource,
  usePreflightBusinessAdoWiki,
  useProviders,
  useSetBusinessAdoCredential,
  useUploadBusinessDocument,
} from "@/hooks/queries";
import { toast } from "@/lib/toast";
import type { BusinessSourceKind, BusinessSourceOut, ConnectionOut } from "@/types/api";

/**
 * The Business Knowledge **add-source** form — per-kind fields (#848, epic #813).
 *
 * The generic *kind / title / URL* form that shipped with the tab (#817) could
 * register a `github_md` or an `ado_wiki` row but never supply what makes one
 * work, so two of the four kinds existed in the backend and could not be set up
 * from the UI at all. Each kind is addressed differently and credentialed
 * differently, and this form says so rather than averaging them:
 *
 * | kind | address | credential |
 * | --- | --- | --- |
 * | `upload` | a `.md`/`.txt` **file** — no URL at all | none |
 * | `url` | one page, fetched once, never crawled | none |
 * | `github_md` | repo / folder / file address | **none for a public repo**; a GitHub connection for a private one |
 * | `ado_wiki` | a wiki address | a wiki-scoped PAT (primary), or a *local* Azure DevOps connection |
 *
 * ## The hub refusal is the point, not a nicety
 *
 * `credentials.credential_origin` on the server answers "where would this
 * source's token come from" — `none | source | connection | hub | missing` —
 * *before* anything is fetched, precisely so this decision can be shown at the
 * field the user is looking at. {@link adoCredentialOrigin} is the same verdict
 * computed one step earlier, from the connection the user is picking right now:
 * a hub-backed connection (`ProviderConnection.hub_connection_id`, surfaced as
 * `ConnectionOut.hubBacked`) holds no PAT and never will (#501), so a wiki read
 * through it is not slow or degraded, it is impossible. The form refuses the
 * submit and says the sentence that names the fix. It is deliberately NOT
 * "reconnect Azure DevOps": that connection is perfectly healthy for the work
 * items it was made for.
 *
 * For `github_md` a hub-backed connection is merely useless rather than fatal —
 * the adapter falls through to an anonymous read, which is exactly right for the
 * common case (a public repository) and 404s with its own "may be private …
 * contents:read" text otherwise. So that one warns and still submits.
 *
 * ## Opaque, tokenised, both modes
 *
 * Rendered inside the tab's `bg-pop` panel; every colour here is an appearance
 * token (#783/#784) — `text-txt4`, `text-warn`, `bg-warn-tint` — so the form is
 * legible on a paper-white page as well as over the dark animated shell, and
 * carries no hardcoded `rgba()` that would only work in one of them (#844).
 */

/** The v1 kinds, in the order the picker offers them. `notion` is v2 (#832). */
export const KINDS: BusinessSourceKind[] = ["url", "github_md", "ado_wiki", "upload"];

/** Where an Azure DevOps wiki source's token would come from — the client-side
 *  twin of `app.services.business_ingest.credentials.credential_origin`, using
 *  the same five words so the form and the server never disagree about which
 *  branch a source is on.
 *
 *  @param pat A wiki-scoped token typed into the form; wins over everything.
 *  @param connection The Azure DevOps connection picked, if any.
 *  @returns `source` (its own token), `connection` (a usable local one), `hub`
 *    (hub-backed — can never supply a PAT) or `missing` (nothing to fetch with).
 */
export function adoCredentialOrigin(
  pat: string,
  connection: ConnectionOut | null,
): "source" | "connection" | "hub" | "missing" {
  if (pat.trim()) return "source";
  if (!connection) return "missing";
  if (connection.hubBacked) return "hub";
  // `secretFields` lists the names of the secrets a connection holds, never the
  // values — so "does it have a PAT at all" is answerable without decrypting
  // anything, which is the same question `credential_origin` asks.
  return connection.secretFields.includes("pat") ? "connection" : "missing";
}

export const inputCls =
  "w-full rounded-[10px] border border-bd2 bg-card px-3 py-2 text-[13px] text-txt3 outline-none placeholder:text-label focus:border-p";

export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[11.5px] font-semibold text-txt4">{label}</span>
      {children}
    </label>
  );
}

/** A one-line explanation under a field. Neutral by default; `tone` marks the
 *  two cases the user must not read past. */
function Note({
  tone = "muted",
  children,
  testId,
}: {
  tone?: "muted" | "warn" | "ok";
  children: React.ReactNode;
  testId?: string;
}) {
  const Icon = tone === "warn" ? AlertTriangle : tone === "ok" ? CheckCircle2 : Info;
  const cls =
    tone === "warn"
      ? "bg-warn-tint text-warn"
      : tone === "ok"
        ? "bg-ok-tint text-ok"
        : "text-faint";
  return (
    <p
      className={`m-0 flex items-start gap-1.5 rounded-lg text-[11.5px] leading-relaxed ${cls} ${tone === "muted" ? "" : "px-2.5 py-2"}`}
      data-testid={testId}
    >
      <Icon size={13} className="mt-[1px] shrink-0" strokeWidth={2.2} />
      <span>{children}</span>
    </p>
  );
}

export function BusinessSourceForm({
  projectGuid,
  onCreated,
  onCancel,
}: {
  projectGuid: string;
  /** Called with the created row once it exists, so the tab can start its sync. */
  onCreated: (row: BusinessSourceOut) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation("projects");
  const create = useCreateBusinessSource(projectGuid);
  const upload = useUploadBusinessDocument(projectGuid);
  const setCredential = useSetBusinessAdoCredential(projectGuid);
  const preflight = usePreflightBusinessAdoWiki(projectGuid);
  const providers = useProviders();
  const fileInput = useRef<HTMLInputElement>(null);

  const [kind, setKind] = useState<BusinessSourceKind>("url");
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [pat, setPat] = useState("");
  const [connectionId, setConnectionId] = useState<number | null>(null);
  const [tested, setTested] = useState<string | null>(null);

  /** The user's connections of one provider kind, from the grouped catalog. */
  const connectionsOf = (providerKind: "github" | "ado"): ConnectionOut[] =>
    providers.data?.find((group) => group.kind === providerKind)?.connections ?? [];

  const githubConnections = connectionsOf("github");
  const adoConnections = connectionsOf("ado");
  const pool = kind === "github_md" ? githubConnections : adoConnections;
  const connection = pool.find((c) => c.id === connectionId) ?? null;
  const origin = adoCredentialOrigin(pat, connection);

  /** Picking a different kind resets what the previous kind's fields meant. */
  const changeKind = (next: BusinessSourceKind) => {
    setKind(next);
    setConnectionId(null);
    setPat("");
    setTested(null);
    if (next === "upload") setUrl("");
    else setFile(null);
  };

  const busy =
    create.isPending || upload.isPending || setCredential.isPending || preflight.isPending;

  // A wiki that would be fetched through a hub-backed connection can only ever
  // fail, so it is refused here rather than registered and left to 401 later.
  const adoBlocked = kind === "ado_wiki" && (origin === "hub" || origin === "missing");
  const canSubmit =
    !busy &&
    (kind === "upload" ? file !== null : url.trim().length > 0) &&
    !adoBlocked;

  const message = (e: unknown, fallback: string) =>
    e instanceof Error && e.message ? e.message : fallback;

  /** Test the wiki address and token without storing either (#822). */
  const testWiki = async () => {
    try {
      const result = await preflight.mutateAsync({ url: url.trim(), pat: pat.trim() });
      setTested(result.wiki || result.project);
    } catch (e) {
      setTested(null);
      toast.error(message(e, t("businessTab.form.ado.testFailed")));
    }
  };

  const submit = async () => {
    try {
      if (kind === "upload") {
        const row = await upload.mutateAsync({
          file: file as File,
          title: title.trim() || undefined,
        });
        toast.success(t("businessTab.form.uploaded", { title: row.title }));
        onCreated(row);
        return;
      }

      // Preflight BEFORE the row exists: a scope problem should be a message at
      // the token field, not a source that sits in the list reading `error`.
      if (kind === "ado_wiki" && pat.trim()) {
        await preflight.mutateAsync({ url: url.trim(), pat: pat.trim() });
      }

      const row = await create.mutateAsync({
        kind,
        title: title.trim() || undefined,
        url: url.trim(),
        connectionId: kind === "url" ? null : connectionId,
      });

      // The token has to land before the sync, or the first fetch runs without
      // it and the row reports a credential failure the user already solved.
      if (kind === "ado_wiki" && pat.trim()) {
        await setCredential.mutateAsync({ id: row.id, pat: pat.trim() });
      }
      toast.success(t("businessTab.form.created", { title: row.title }));
      onCreated(row);
    } catch (e) {
      toast.error(message(e, t("businessTab.form.error")));
    }
  };

  return (
    <div
      className="flex flex-col gap-3 border-b border-bd3 px-5 py-4"
      data-testid="business-add-form"
      data-kind={kind}
    >
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <Field label={t("businessTab.form.kind")}>
          <Select
            value={kind}
            options={KINDS.map((k) => ({ value: k, label: t(`businessTab.kinds.${k}`) }))}
            placeholder={t("businessTab.form.kindPlaceholder")}
            allowClear={false}
            fullWidth
            onChange={(v) => v && changeKind(v as BusinessSourceKind)}
          />
        </Field>
        <Field label={t("businessTab.form.title")}>
          <input
            className={inputCls}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder={t("businessTab.form.titlePlaceholder")}
            data-testid="business-title-input"
          />
        </Field>
      </div>

      {kind === "upload" ? (
        <>
          <Field label={t("businessTab.form.upload.file")}>
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="ghost" size="sm" onClick={() => fileInput.current?.click()}>
                <Paperclip size={14} strokeWidth={2.2} />
                {t("businessTab.form.upload.choose")}
              </Button>
              <span className="text-[12px] text-txt4" data-testid="business-file-name">
                {file ? file.name : t("businessTab.form.upload.none")}
              </span>
              <input
                ref={fileInput}
                type="file"
                accept=".md,.txt,text/markdown,text/plain"
                className="hidden"
                data-testid="business-file-input"
                onChange={(e) => {
                  const picked = e.target.files?.[0] ?? null;
                  setFile(picked);
                  if (picked && !title.trim()) setTitle(picked.name);
                }}
              />
            </div>
          </Field>
          <Note>{t("businessTab.form.upload.hint")}</Note>
        </>
      ) : (
        <>
          <Field label={t("businessTab.form.url")}>
            <input
              className={inputCls}
              value={url}
              onChange={(e) => {
                setUrl(e.target.value);
                setTested(null);
              }}
              placeholder={t(`businessTab.form.urlPlaceholder.${kind}`)}
              data-testid="business-url-input"
            />
          </Field>
          <Note>{t(`businessTab.form.addressHint.${kind}`)}</Note>
        </>
      )}

      {kind === "github_md" && (
        <>
          <Field label={t("businessTab.form.github.connection")}>
            <Select
              value={connectionId === null ? null : String(connectionId)}
              options={githubConnections.map((c) => ({
                value: String(c.id),
                label: c.hubBacked
                  ? t("businessTab.form.github.hubOption", { name: c.name })
                  : c.name,
              }))}
              placeholder={t("businessTab.form.github.public")}
              emptyLabel={t("businessTab.form.github.noConnections")}
              fullWidth
              onChange={(v) => setConnectionId(v ? Number(v) : null)}
            />
          </Field>
          {connection?.hubBacked ? (
            <Note tone="warn" testId="business-github-hub">
              {t("businessTab.form.github.hubNote")}
            </Note>
          ) : (
            <Note>{t("businessTab.form.github.publicNote")}</Note>
          )}
          <Note>{t("businessTab.form.github.refNote")}</Note>
        </>
      )}

      {kind === "ado_wiki" && (
        <>
          <Field label={t("businessTab.form.ado.pat")}>
            <input
              className={inputCls}
              type="password"
              autoComplete="off"
              value={pat}
              onChange={(e) => {
                setPat(e.target.value);
                setTested(null);
              }}
              placeholder={t("businessTab.form.ado.patPlaceholder")}
              data-testid="business-pat-input"
            />
          </Field>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="ghost"
              size="sm"
              disabled={!url.trim() || !pat.trim() || preflight.isPending}
              onClick={testWiki}
              data-testid="business-ado-test"
            >
              {preflight.isPending
                ? t("businessTab.form.ado.testing")
                : t("businessTab.form.ado.test")}
            </Button>
            {tested && (
              <Note tone="ok" testId="business-ado-tested">
                {t("businessTab.form.ado.testOk", { wiki: tested })}
              </Note>
            )}
          </div>
          <Field label={t("businessTab.form.ado.connection")}>
            <Select
              value={connectionId === null ? null : String(connectionId)}
              options={adoConnections.map((c) => ({
                value: String(c.id),
                label: c.hubBacked
                  ? t("businessTab.form.github.hubOption", { name: c.name })
                  : c.name,
              }))}
              placeholder={t("businessTab.form.ado.noConnection")}
              emptyLabel={t("businessTab.form.ado.noConnections")}
              fullWidth
              onChange={(v) => setConnectionId(v ? Number(v) : null)}
            />
          </Field>
          {origin === "hub" ? (
            <Note tone="warn" testId="business-ado-hub">
              {t("businessTab.form.ado.hubRefusal")}
            </Note>
          ) : origin === "missing" ? (
            <Note tone="warn" testId="business-ado-missing">
              {t("businessTab.form.ado.missing")}
            </Note>
          ) : origin === "connection" ? (
            <Note testId="business-ado-connection">
              {t("businessTab.form.ado.connectionNote")}
            </Note>
          ) : (
            <Note testId="business-ado-source">{t("businessTab.form.ado.sourceNote")}</Note>
          )}
        </>
      )}

      <div className="flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={onCancel}>
          {t("businessTab.cancel")}
        </Button>
        <Button
          variant="primary"
          size="sm"
          disabled={!canSubmit}
          onClick={submit}
          data-testid="business-submit"
        >
          {busy ? t("businessTab.form.submitting") : t("businessTab.form.submit")}
        </Button>
      </div>
    </div>
  );
}
