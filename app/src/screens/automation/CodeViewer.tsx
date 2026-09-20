import { ChevronDown, ChevronRight } from "lucide-react";
import { useEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import { usePrefersReducedMotion } from "@/hooks/usePrefersReducedMotion";

const TS_KEYWORDS = new Set([
  "import", "export", "from", "const", "let", "var", "async", "await", "function",
  "test", "expect", "describe", "it", "return", "if", "else", "new", "class",
  "extends", "interface", "type", "for", "of", "in", "typeof",
]);
const TS_KEYWORD_SPLIT = /\b([a-zA-Z]+)\b/g;

/** A collapsible region of code, identified by its opener and closer line indices (0-based). */
export type FoldRange = { start: number; end: number };

const CLOSE_FOR: Record<string, string> = { "{": "}", "(": ")", "[": "]" };
const OPEN_FOR: Record<string, string> = { "}": "{", ")": "(", "]": "[" };

/**
 * Derive foldable brace/bracket regions from source code.
 *
 * Scans the code character by character while skipping strings, line comments,
 * and block comments (best-effort, never throws), tracking a stack of open
 * bracket positions. When a matching closer is found on a later line the span
 * is recorded. Only the widest region per opener line is kept, so each opener
 * folds down to its furthest matching closer. Single-line pairs are not
 * foldable.
 *
 * @param code Full spec source text.
 * @returns Fold ranges sorted by opener line, each spanning 2+ lines.
 */
export function computeFoldRanges(code: string): FoldRange[] {
  const lines = code.split("\n");
  const stack: { char: string; line: number }[] = [];
  const endByStart = new Map<number, number>();

  let inStr: string | null = null;
  let inBlockComment = false;

  for (let li = 0; li < lines.length; li++) {
    const line = lines[li];
    for (let ci = 0; ci < line.length; ci++) {
      const ch = line[ci];
      const next = line[ci + 1];

      if (inBlockComment) {
        if (ch === "*" && next === "/") {
          inBlockComment = false;
          ci++;
        }
        continue;
      }
      if (inStr) {
        if (ch === "\\") ci++;
        else if (ch === inStr) inStr = null;
        continue;
      }
      if (ch === "/" && next === "/") break; // line comment: skip rest of line
      if (ch === "/" && next === "*") {
        inBlockComment = true;
        ci++;
        continue;
      }
      if (ch === "'" || ch === '"' || ch === "`") {
        inStr = ch;
        continue;
      }
      if (ch === "{" || ch === "(" || ch === "[") {
        stack.push({ char: ch, line: li });
        continue;
      }
      if (ch === "}" || ch === ")" || ch === "]") {
        const open = OPEN_FOR[ch];
        let idx = stack.length - 1;
        while (idx >= 0 && stack[idx].char !== open) idx--;
        if (idx >= 0) {
          const opener = stack[idx];
          stack.length = idx; // pop the match and any unclosed openers above it
          if (li > opener.line) {
            const prev = endByStart.get(opener.line);
            if (prev === undefined || li > prev) endByStart.set(opener.line, li);
          }
        }
      }
    }
  }

  return [...endByStart.entries()]
    .map(([start, end]) => ({ start, end }))
    .sort((a, b) => a.start - b.start);
}

/** Closing bracket char for a fold's opener line (from its last non-space char). */
function closerCharFor(line: string): string {
  const trimmed = line.trimEnd();
  return CLOSE_FOR[trimmed[trimmed.length - 1]] ?? "}";
}

/**
 * Read-only TypeScript viewer with brace-based code folding.
 *
 * Renders every line with a sticky left gutter (line number + fold chevron on
 * opener lines) and the existing token highlighting. Lines inside a collapsed
 * region are hidden; the opener line gets an inline "N lines" marker. The outer
 * container keeps horizontal scrolling for long lines.
 *
 * @param code Full spec source text.
 * @param foldRanges Precomputed foldable regions for this code.
 * @param folded Set of opener line indices that are currently collapsed.
 * @param onToggle Toggles the fold state of the region opening at a line.
 * @param changedLines Optional 0-based indices of lines added/changed by the last
 *   regeneration or chat edit — tinted green with a `+` gutter marker (folding is
 *   unaffected).
 * @param scrollToLine Optional 0-based line to scroll into view (the first changed
 *   line of a chat edit). Scrolls when `scrollSignal` changes.
 * @param scrollSignal Bumps per applied chat edit so a repeated `scrollToLine`
 *   still re-triggers the scroll.
 */
export function CodeHighlight({
  code,
  foldRanges,
  folded,
  onToggle,
  changedLines,
  scrollToLine,
  scrollSignal,
}: {
  code: string;
  foldRanges: FoldRange[];
  folded: Set<number>;
  onToggle: (start: number) => void;
  changedLines?: Set<number>;
  scrollToLine?: number;
  scrollSignal?: number;
}) {
  const { t } = useTranslation("pipeline");
  const lines = code.split("\n");
  const endByStart = useMemo(() => new Map(foldRanges.map((r) => [r.start, r.end])), [foldRanges]);

  // Scroll the first edited line into view when a chat edit lands. Keyed on
  // scrollSignal (not scrollToLine) so consecutive edits touching the same line
  // still re-scroll; instant under prefers-reduced-motion. `code` is a dep too so
  // that when the target line isn't rendered yet (the re-typed code is still
  // filling in), we retry on the next frame; the ref guard fires the scroll once
  // per signal, the first render where the target line actually exists.
  const reducedMotion = usePrefersReducedMotion();
  const scrollTargetRef = useRef<HTMLDivElement | null>(null);
  const lastScrolledSignal = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (scrollSignal === undefined || scrollToLine === undefined) return;
    if (lastScrolledSignal.current === scrollSignal) return;
    const el = scrollTargetRef.current;
    if (!el) return;
    lastScrolledSignal.current = scrollSignal;
    el.scrollIntoView({ block: "center", behavior: reducedMotion ? "auto" : "smooth" });
  }, [scrollSignal, scrollToLine, code, reducedMotion]);

  // Line indices hidden because they sit inside a currently-collapsed region.
  const hidden = useMemo(() => {
    const set = new Set<number>();
    for (const start of folded) {
      const end = endByStart.get(start);
      if (end === undefined) continue;
      for (let i = start + 1; i <= end; i++) set.add(i);
    }
    return set;
  }, [folded, endByStart]);

  // The gutter is `position: sticky` over horizontally-scrolling code, so it has
  // to be OPAQUE or the code slides visibly underneath it. `--pop` is the token
  // for exactly that (#191921 dark / #ffffff light); the changed-line variant
  // layers `--okTint` over the same opaque base rather than hard-coding a second
  // near-black, which is why it is a two-stop background rather than one colour.
  const gutterBg = "var(--pop)";
  const gutterChangedBg = "linear-gradient(var(--okTint),var(--okTint)), var(--pop)";

  return (
    <div className="overflow-x-auto font-mono text-[12.5px] leading-[1.75] text-txt">
      <div className="min-w-max py-[18px]">
        {lines.map((line, i) => {
          if (hidden.has(i)) return null;
          const end = endByStart.get(i);
          const isFoldable = end !== undefined;
          const isFolded = isFoldable && folded.has(i);
          const isChanged = changedLines?.has(i) ?? false;
          return (
            <div
              key={i}
              ref={i === scrollToLine ? scrollTargetRef : undefined}
              className="flex"
              style={isChanged ? { background: "var(--okTint)" } : undefined}
            >
              <span
                className="sticky left-0 z-10 flex select-none items-center gap-1 pl-4 pr-3"
                style={{ background: isChanged ? gutterChangedBg : gutterBg }}
              >
                {isFoldable ? (
                  <button
                    type="button"
                    onClick={() => onToggle(i)}
                    className="flex h-[14px] w-[14px] items-center justify-center text-faint hover:text-txt3"
                    aria-label={isFolded ? t("spec.code.expandRegion") : t("spec.code.collapseRegion")}
                  >
                    {isFolded ? <ChevronRight size={13} /> : <ChevronDown size={13} />}
                  </button>
                ) : isChanged ? (
                  <span className="flex h-[14px] w-[14px] items-center justify-center font-bold text-ok">+</span>
                ) : (
                  <span className="h-[14px] w-[14px]" />
                )}
                <span className="w-8 text-right text-faint">{i + 1}</span>
              </span>
              <span className="whitespace-pre pl-3 pr-5">
                {highlightLine(line) || " "}
                {isFolded ? (
                  <span className="text-faint">
                    {" "}
                    &#8943; {t("spec.code.foldedLines", { count: end - i })} {closerCharFor(line)}
                  </span>
                ) : null}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function highlightLine(line: string) {
  const tokens: { text: string; cls?: string }[] = [];
  const pattern = /(\/\/.*$)|('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = pattern.exec(line))) {
    if (m.index > last) tokens.push({ text: line.slice(last, m.index) });
    tokens.push({ text: m[0], cls: m[1] ? "cmt" : "str" });
    last = m.index + m[0].length;
  }
  if (last < line.length) tokens.push({ text: line.slice(last) });

  return tokens.map((t, i) => {
    if (t.cls === "cmt") return <span key={i} style={{ color: "var(--label)" }}>{t.text}</span>;
    if (t.cls === "str") return <span key={i} style={{ color: "var(--ok)" }}>{t.text}</span>;
    const parts = t.text.split(TS_KEYWORD_SPLIT);
    return (
      <span key={i}>
        {parts.map((p, j) =>
          TS_KEYWORDS.has(p) ? (
            <span key={j} style={{ color: "var(--psText)" }}>
              {p}
            </span>
          ) : (
            p
          ),
        )}
      </span>
    );
  });
}
