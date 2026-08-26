import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Spinner } from "@/components/ui/misc";
import { useCommentPreview } from "@/hooks/queries";
import { api } from "@/lib/api";

/**
 * The comment as the **provider** will render it (#707).
 *
 * The card used to show the raw draft, so a reviewer approved something that was not
 * what the work item would display: `![TC-01](shot.png)` appeared as literal Markdown,
 * and the screenshot the template deliberately places under its finding was invisible
 * in the one screen where someone decides whether the comment is right.
 *
 * **The HTML comes from the server** — `comment_markup.to_html`, the same function the
 * adapter posts through. A TypeScript twin would drift, and a preview that drifts is
 * worse than no preview: it is confidently wrong.
 *
 * Two things the first version got wrong, both visible only by looking at it:
 *
 * * **The images were broken.** `/artifacts/**` needs a short-lived access token that
 *   lives only in this browser's memory, and the URL needs the SPA's mount prefix — the
 *   server knows neither. It now emits the artifact path in `data-artifact` and the
 *   real source is filled in here, through `api.artifactUrl`, which knows both.
 * * **The surface was hardcoded white.** ADO renders comments on white, but the app
 *   does not, and a white slab in a dark shell is a worse mismatch than the one it was
 *   imitating. It now uses the app's own tokens, so it follows the shell — including
 *   whatever a future light theme does, without this file changing.
 */
export function CommentPreview({ commentId, body }: { commentId: number; body: string }) {
  const { t } = useTranslation("pipeline");
  const { data, isLoading, isError } = useCommentPreview(commentId, body);

  // Resolve `data-artifact` → a URL this browser can actually load. Parsed as a
  // document rather than string-replaced: the server escaped this HTML, and putting a
  // token into it by regex is how an escape gets undone by accident.
  const html = useMemo(() => {
    if (!data?.html) return "";
    const doc = new DOMParser().parseFromString(data.html, "text/html");
    doc.querySelectorAll<HTMLImageElement>("img[data-artifact]").forEach((img) => {
      const path = img.getAttribute("data-artifact");
      if (path) img.setAttribute("src", api.artifactUrl(path));
    });
    return doc.body.innerHTML;
  }, [data?.html]);

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 p-3 text-[12.5px] text-ink-dim md:p-[14px_18px]">
        <Spinner size={13} /> {t("publish.preview.loading")}
      </div>
    );
  }

  if (isError || !html) {
    // Fall back to the raw body rather than an empty card: the draft is still the
    // thing being reviewed, and showing nothing hides it entirely.
    return (
      <div className="p-3 md:p-[14px_18px]">
        <div className="mb-2 text-[11.5px] text-warning-soft">{t("publish.preview.failed")}</div>
        <pre className="m-0 whitespace-pre-wrap font-mono text-[12px] leading-relaxed text-ink-soft">
          {body}
        </pre>
      </div>
    );
  }

  return (
    <div className="p-3 md:p-[14px_18px]">
      <div
        className="comment-preview overflow-x-auto rounded-[11px] border border-white/[0.07] bg-white/[0.02] p-4"
        // The HTML is our own render of our own draft, escaped by `comment_markup`
        // before any user text reaches it — the same escaping that protects the work
        // item from whatever a reviewer types into the edit box.
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </div>
  );
}
