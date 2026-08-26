"""Markdown → HTML for providers whose comment field renders HTML (#703).

Q-Agent stores a comment draft as **Markdown**: that is what the Publish screen renders
with `MarkdownLite`, and what a reviewer edits. Azure DevOps work-item comments render
**HTML**, and the draft was being posted into them verbatim — so `**PASSED**` reached
the work item as literal asterisks and `![shot](file.png)` as literal brackets.

Nobody caught it because the draft looks right in Q-Agent and wrong only in the ticket,
which is the one place it matters and the one place we were not looking.

Deliberately a *small* converter rather than a Markdown library. It handles exactly the
constructs :mod:`app.services.comment_template` emits — bold, an ordered list whose
items carry indented continuation lines, nested bullets, images, and paragraphs — and
escapes everything else. A general Markdown renderer would also faithfully turn a stray
underscore in a test name into emphasis, and would hand a provider whatever raw HTML a
reviewer typed into the edit box.

**Indentation is structure here.** An observation and its screenshot are indented under
their numbered item, and they have to stay *inside* that ``<li>`` — rendering them as
sibling blocks pulls the evidence away from the finding it belongs to, which is the
exact arrangement the template exists to avoid.
"""

from __future__ import annotations

import html
import re

__all__ = ["to_html"]

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_NUMBERED = re.compile(r"^(\d+)\.\s+(.*)$")
_BULLET = re.compile(r"^(\s*)-\s+(.*)$")


def to_html(markdown: str, *, image_src: dict[str, str] | None = None) -> str:
    """Render ``markdown`` as the HTML subset a work-item comment accepts.

    ``image_src`` maps an image target (the attachment filename the template wrote) to
    the URL it ended up at. An image whose target is **not** in the map is dropped
    rather than emitted with a dead source: a broken-image icon in a ticket reads as
    "the evidence is gone", which is a worse lie than the line not being there.
    """
    sources = image_src or {}
    out: list[str] = []
    # The open list, if any: "ol" | "ul", plus whether a <li> is still open so an
    # indented continuation line can be folded into it.
    list_kind: str | None = None
    item_open = False

    def close_item() -> None:
        nonlocal item_open
        if item_open:
            out.append("</li>")
            item_open = False

    def close_list() -> None:
        nonlocal list_kind
        close_item()
        if list_kind:
            out.append(f"</{list_kind}>")
            list_kind = None

    for raw_line in (markdown or "").splitlines():
        if not raw_line.strip():
            continue
        indented = raw_line[:1].isspace()
        stripped = raw_line.strip()

        numbered = _NUMBERED.match(stripped)
        bullet = _BULLET.match(raw_line)

        if numbered and not indented:
            # The list marker itself is dropped: <ol> supplies the number, and keeping
            # the literal "1." next to it prints it twice.
            if list_kind != "ol":
                close_list()
                out.append("<ol>")
                list_kind = "ol"
            close_item()
            out.append(f"<li>{_inline(numbered.group(2), sources)}")
            item_open = True
            continue

        if bullet:
            nested = len(bullet.group(1)) >= 2
            if nested and item_open:
                # One level of nesting, folded into the item it belongs to.
                out.append(f"<br/>&nbsp;&nbsp;• {_inline(bullet.group(2), sources)}")
                continue
            if list_kind != "ul":
                close_list()
                out.append("<ul>")
                list_kind = "ul"
            close_item()
            out.append(f"<li>{_inline(bullet.group(2), sources)}")
            item_open = True
            continue

        if indented and item_open:
            # A continuation line — the observation, or the screenshot under it. It
            # stays inside the <li> so the evidence sits with its finding.
            out.append(f"<br/>{_inline(stripped, sources)}")
            continue

        close_list()
        rendered = _inline(stripped, sources)
        out.append(f"<div>{rendered}</div>")

    close_list()
    return "".join(out)


def _inline(text: str, sources: dict[str, str]) -> str:
    """Escape one line, then re-introduce the handful of constructs we emit.

    Order matters: escaping first means a reviewer who types ``<script>`` into the edit
    box gets the characters they typed, not a tag in someone's work item.
    """
    escaped = html.escape(text)

    def image(match: re.Match[str]) -> str:
        alt, target = match.group(1), html.unescape(match.group(2))
        url = sources.get(target)
        if not url:
            return ""  # see the note on dead sources in `to_html`
        return f'<img src="{html.escape(url)}" alt="{alt}" style="max-width:100%"/>'

    # The image pattern runs over the ESCAPED text so everything around it stays escaped.
    with_images = _IMAGE.sub(image, escaped)
    return _BOLD.sub(r"<b>\1</b>", with_images)
