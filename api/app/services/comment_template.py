"""The shape a QC comment takes on a work item (#703).

Modelled directly on the comments Surency's QC writes by hand:

    Hi @jane.doe QC verifies this ticket. Could you review this test report below and
    change the status if there are no issues?
    ENV: QA
    Status: PASSED
    Operating system: Windows
    Browser: Chrome
    Actual result:
      1. <what was observed>
         <screenshot, inline>
      2. <what was observed>
         <screenshot, inline>

Three things about that shape are load-bearing, and each is why this module exists
rather than the prose the model used to return on its own:

* **The header block is scannable.** ENV / Status / OS / Browser on four lines is how a
  reviewer decides in two seconds whether the report is even about their environment.
  Buried in a paragraph, it is not.
* **Every field is a fact from the run, never the model's.** A comment that states the
  wrong browser is worse than one that states none, so an unknown field is *omitted* —
  see :func:`header_lines`.
* **The screenshot sits under its own numbered item.** A list of filenames at the bottom
  makes the reader match evidence to finding by hand, which is exactly the work the
  inline form removes.

The model still writes the *prose* — the per-case observation and the summary — because
that is judgement. Everything structural is assembled here.

Draft bodies stay Markdown (the Publish screen renders them with `MarkdownLite`); the
conversion to a provider's own format happens at publish time, in the adapter.
"""

from __future__ import annotations

import platform
from typing import Any

__all__ = [
    "build_body",
    "header_lines",
    "os_label",
    "browser_label",
]

#: `Execution.browser` holds Playwright's engine id; a QC comment names the browser.
_BROWSER_LABEL = {
    "chromium": "Chrome",
    "chrome": "Chrome",
    "firefox": "Firefox",
    "webkit": "Safari",
    "msedge": "Edge",
    "edge": "Edge",
}

#: `sys.platform`/`platform.system()` values → what a person calls the OS.
_OS_LABEL = {
    "windows": "Windows",
    "darwin": "macOS",
    "linux": "Linux",
    "win32": "Windows",
}


def browser_label(browser: str) -> str:
    """"chromium" → "Chrome". Unknown engines pass through capitalised."""
    key = (browser or "").strip().lower()
    if not key:
        return ""
    return _BROWSER_LABEL.get(key, key.title())


def os_label(raw: str) -> str:
    """A platform string → what a person calls it, or "" when it is not one.

    Empty in, empty out, deliberately: an agent that has never reported its platform
    must produce a *missing* line, not a guess.
    """
    key = (raw or "").strip().lower()
    if not key:
        return ""
    return _OS_LABEL.get(key, raw.strip())


def server_os() -> str:
    """The OS this server runs on — the truthful answer for a server-executed run.

    For an agent-executed run it is the wrong answer (the tests ran on someone's
    laptop), which is why the caller passes the device's platform instead when it has
    one and omits the line when it does not.
    """
    return os_label(platform.system())


def header_lines(
    *,
    env: str,
    status: str,
    operating_system: str,
    browser: str,
) -> list[str]:
    """The ENV / Status / Operating system / Browser block, minus anything unknown.

    Omission is the whole point. Every one of these is a claim about how the test was
    run, and a reader acts on them — "Operating system: Windows" on a run that happened
    in a Linux container sends someone chasing a platform bug that does not exist.
    """
    fields = (
        ("ENV", env),
        ("Status", status),
        ("Operating system", operating_system),
        ("Browser", browser),
    )
    return [f"**{name}:** {value}" for name, value in fields if str(value).strip()]


def build_body(
    *,
    assignee: str,
    env: str,
    status: str,
    operating_system: str,
    browser: str,
    results: list[dict[str, Any]],
    summary: str = "",
    evidence: str = "",
) -> str:
    """Assemble the comment, in QC's shape, as Markdown.

    ``results`` is one entry per test case, in order:
    ``{caseCode, title, status, observation, screenshot}`` — where ``screenshot`` is the
    attachment filename to inline underneath, or "" for a case that captured none. The
    filename is a *placeholder*: the adapter swaps it for the real embed once the file
    has been uploaded (only the provider knows the URL). See
    :meth:`AzureDevOpsAdapter.publish_comment`.

    ``summary`` is the model's consolidated prose, appended after the numbered list so
    the structure a reader scans comes first.

    ``evidence`` is the full per-case artifact manifest (#696). Screenshots already
    appear inline above, but a video or a trace has no inline form — and #696's promise
    is that every case's evidence is *named*, so it goes at the bottom rather than
    being dropped for the sake of the template's shape.
    """
    lines: list[str] = [_greeting(assignee)]
    lines.extend(header_lines(env=env, status=status, operating_system=operating_system, browser=browser))

    lines.append("**Actual result:**")
    if not results:
        # Said out loud. A report with an empty "Actual result" heading reads as a
        # broken comment; "no test cases were executed" reads as a fact about the run.
        lines.append("No test cases were executed for this work item.")
    for index, result in enumerate(results, start=1):
        mark = "PASS" if result.get("status") == "pass" else "FAIL"
        title = str(result.get("title") or "").strip()
        heading = f"{index}. **{result.get('caseCode', '')}** {title} — **{mark}**"
        lines.append(heading)
        observation = str(result.get("observation") or "").strip()
        if observation:
            lines.append(f"   {observation}")
        screenshot = str(result.get("screenshot") or "").strip()
        if screenshot:
            # Markdown image syntax so the draft previews in the Publish screen; the
            # adapter rewrites it to the uploaded attachment at publish time.
            lines.append(f"   ![{result.get('caseCode', 'evidence')}]({screenshot})")

    if summary.strip():
        lines.append("**Summary:**")
        lines.append(summary.strip())

    if evidence.strip():
        lines.append(evidence.strip())

    return "\n".join(lines)


def _greeting(assignee: str) -> str:
    """The opening line, addressed to whoever owns the work item.

    A bare "QC verifies this ticket" with no addressee is a notice; naming the assignee
    and asking for the status change is a request someone acts on, which is what QC's
    own comments do. With no assignee on the ticket, the ask stands without the name
    rather than addressing nobody as "@".
    """
    who = (assignee or "").strip()
    lead = f"Hi @{who}" if who else "Hi"
    return (
        f"{lead} — QC verifies this ticket. Could you review this test report below and "
        "change the status if there are no issues?"
    )
