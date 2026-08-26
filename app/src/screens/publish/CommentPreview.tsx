import { useTranslation } from "react-i18next";
import { Spinner } from "@/components/ui/misc";
import { useCommentPreview } from "@/hooks/queries";

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
 * The light surface is deliberate too. Azure DevOps renders comments on white, and a
 * screenshot of a light web app on this shell's near-black card is not what the reader
 * will see — the point of a preview is to look like the destination.
 */
export function CommentPreview({ commentId, body }: { commentId: number; body: string }) {
  const { t } = useTranslation("pipeline");
  const { data, isLoading, isError } = useCommentPreview(commentId, body);

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 p-3 text-[12.5px] text-ink-dim md:p-[14px_18px]">
        <Spinner size={13} /> {t("publish.preview.loading")}
      </div>
    );
  }

  if (isError || !data?.html) {
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
        className="comment-preview overflow-x-auto rounded-[11px] p-4"
        style={{ background: "#ffffff", color: "#1f2328" }}
        // The HTML is our own render of our own draft, escaped by `comment_markup`
        // before any user text reaches it — the same escaping that protects the work
        // item from whatever a reviewer types into the edit box.
        dangerouslySetInnerHTML={{ __html: data.html }}
      />
    </div>
  );
}
