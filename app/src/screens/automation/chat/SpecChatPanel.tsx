/**
 * AI chat panel to edit the selected spec (Automation screen). A 480px right
 * slide-in drawer: the reviewer types an instruction, Claude really edits the
 * spec (backend `POST /cases/{id}/spec/chat`), the reply types out char-by-char,
 * a red/green diff shows what changed, and the spec is updated with Undo.
 *
 * Mounted inside Automation.tsx (under RunSocketProvider) so `useRunEvents`
 * receives the `automation.chat.reply` / `automation.chat.error` results.
 */
import { AnimatePresence, motion } from "framer-motion";
import { Send, Sparkles, Undo2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { createPortal } from "react-dom";
import { useRunEvents } from "@/hooks/useRunEvents";
import { useAuthoringState, useSendSpecChat, useSpecs, useUpdateSpec } from "@/hooks/queries";
import { AI_MODEL_OPTIONS } from "@/lib/models";
import { useUI } from "@/store/ui";
import type { AutomationSpecOut, ChatErrorPayload, ChatReplyPayload } from "@/types/api";
import { diffLines } from "@/screens/automation/lineDiff";
import { useTypewriter } from "./useTypewriter";

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  thinking?: boolean;
  error?: string;
  prevCode?: string;
  code?: string;
  applied?: boolean;
  reverted?: boolean;
};

interface Props {
  runId: number;
  spec: AutomationSpecOut | null | undefined;
}

/** Drives the chat message list + send/undo for the currently-selected spec. */
export function SpecChatPanel({ runId, spec }: Props) {
  const { t } = useTranslation("pipeline");
  const quickActions = [
    t("automation.chat.quickUseTestId"),
    t("automation.chat.quickNetworkIdle"),
    t("automation.chat.quickAssertToast"),
    t("automation.chat.quickEmptyState"),
  ];
  const chatOpen = useUI((s) => s.chatOpen);
  const closeChat = useUI((s) => s.closeChat);
  const sendChat = useSendSpecChat(runId);
  const updateSpec = useUpdateSpec(runId);
  const caseId = spec?.testCaseId ?? 0;

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [model, setModel] = useState<string>(AI_MODEL_OPTIONS[1]?.value ?? "");
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Message ids whose reply has already typed out once — so closing + reopening
  // the panel shows them in full instantly (no re-typing of old messages).
  const revealedRef = useRef<Set<string>>(new Set());

  // @-mention: the run's specs the reviewer can embed as context. `mention` is the
  // active token (the text after the last "@") + where it starts, or null.
  const specs = useSpecs(runId).data ?? [];
  // #619: while live authoring holds this case the panel is the GUIDANCE channel,
  // not the spec-edit channel — the server banks the message on the authoring
  // session and no `automation.chat.reply` will ever arrive for it.
  const authoring = useAuthoringState(caseId, caseId > 0).data;
  const guidanceMode = Boolean(authoring?.active);
  const [mention, setMention] = useState<{ start: number; query: string } | null>(null);
  const mentionMatches = mention
    ? specs.filter((s) => s.filename.toLowerCase().includes(mention.query.toLowerCase())).slice(0, 8)
    : [];

  // Reset the conversation when the selected spec changes.
  useEffect(() => {
    setMessages([]);
    setInput("");
  }, [caseId]);

  // Auto-scroll to the newest message.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  const busy = messages.some((m) => m.role === "assistant" && m.thinking);

  const patch = (id: string, next: Partial<ChatMessage>) =>
    setMessages((ms) => ms.map((m) => (m.id === id ? { ...m, ...next } : m)));

  // The real edit result arrives over the run WS (correlated by messageId).
  useRunEvents((evt) => {
    if (evt.event === "automation.chat.reply") {
      const p = evt.payload as unknown as ChatReplyPayload;
      if (p.caseId !== caseId) return;
      patch(p.messageId, {
        thinking: false,
        text: p.text,
        prevCode: p.prevCode,
        code: p.spec.code,
        applied: true,
      });
    } else if (evt.event === "automation.chat.error") {
      const p = evt.payload as unknown as ChatErrorPayload;
      if (p.caseId !== caseId) return;
      patch(p.messageId, { thinking: false, error: p.error });
    }
  });

  const send = (raw: string) => {
    const message = raw.trim();
    if (!message || !spec || busy) return;
    const messageId =
      typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `m-${messages.length}`;
    setMessages((ms) => [
      ...ms,
      { id: `u-${messageId}`, role: "user", text: message },
      { id: messageId, role: "assistant", text: "", thinking: true },
    ]);
    setInput("");
    setMention(null);
    sendChat.mutate(
      { caseId, message, model: model || undefined, messageId },
      {
        // A message routed to authoring gets NO WS reply, so its pending bubble
        // must be resolved from this response. Without this the panel would spin
        // forever and `busy` would lock the input — the exact failure a naive
        // "just route it server-side" change produces.
        onSuccess: (res) => {
          if (res?.routedToAuthoring) {
            patch(messageId, {
              thinking: false,
              text: t("progress.authoring.guidanceQueued"),
            });
          }
        },
        onError: (err) =>
          patch(messageId, {
            thinking: false,
            error: (err as { message?: string })?.message || String(err),
          }),
      },
    );
  };

  // Detect an active "@…" token immediately before the caret, to drive the
  // spec-mention dropdown.
  const detectMention = (value: string, caret: number) => {
    const m = /@([\w.\-]*)$/.exec(value.slice(0, caret));
    setMention(m ? { start: caret - m[0].length, query: m[1] } : null);
  };

  // Replace the active "@query" with "@<filename> " so the referenced spec is
  // embedded in the message (the backend resolves the mention to that spec's code).
  const insertMention = (filename: string) => {
    if (!mention) return;
    const to = mention.start + 1 + mention.query.length;
    const next = `${input.slice(0, mention.start)}@${filename} ${input.slice(to)}`;
    setInput(next);
    setMention(null);
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (el) {
        const pos = mention.start + filename.length + 2;
        el.focus();
        el.setSelectionRange(pos, pos);
      }
    });
  };

  const undo = (m: ChatMessage) => {
    if (m.prevCode == null) return;
    updateSpec.mutate({ caseId, code: m.prevCode });
    patch(m.id, { applied: false, reverted: true });
  };
  const reapply = (m: ChatMessage) => {
    if (m.code == null) return;
    updateSpec.mutate({ caseId, code: m.code });
    patch(m.id, { applied: true, reverted: false });
  };

  // No dimming/blocking backdrop — the page (and the code editor) stay fully
  // visible and bright behind the drawer, so the reviewer can WATCH the edited
  // code type into the editor while chatting. Close via the X or Escape (a
  // full-screen scrim would cover/darken the editor). The wrapper is
  // pointer-events-none so only the drawer captures clicks; the editor behind
  // stays interactive.
  return createPortal(
    <AnimatePresence>
      {chatOpen && spec && (
        <motion.div
          key="chat-scrim"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.2 }}
          className="pointer-events-none fixed inset-0 z-[1000]"
        >
          <motion.aside
            key="chat-drawer"
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ duration: 0.28, ease: [0.2, 0.8, 0.2, 1] }}
            className="pointer-events-auto absolute right-0 top-0 flex h-full w-[480px] max-w-[96vw] flex-col border-l border-white/[0.09] shadow-[0_0_60px_-10px_rgba(0,0,0,.7)]"
            style={{ background: "#0e0e15" }}
          >
            {/* Header */}
            <div className="flex items-center gap-3 border-b border-white/[0.07] px-4 py-3">
              <span className="accent-gradient flex h-8 w-8 items-center justify-center rounded-lg">
                <Sparkles size={16} color="#fff" />
              </span>
              <div className="min-w-0">
                <div className="text-[13px] font-bold leading-tight">Q-Agent</div>
                <div className="truncate font-mono text-[11px] text-ink-dim">{spec.filename}</div>
              </div>
              <select
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className="ml-auto rounded-lg border border-white/[0.1] bg-white/[0.04] px-2 py-1 text-[11px] text-ink-soft"
                title={t("automation.chat.model")}
              >
                {AI_MODEL_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label.split(" — ")[0]}
                  </option>
                ))}
              </select>
              <button
                onClick={closeChat}
                className="flex h-7 w-7 items-center justify-center rounded-lg text-ink-dim hover:bg-white/[0.08]"
                title={t("automation.chat.close")}
              >
                <X size={16} />
              </button>
            </div>

            {/* Messages */}
            <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto p-4">
              {messages.length === 0 && (
                <div className="px-2 py-8 text-center text-[12.5px] text-ink-dim">
                  {t("automation.chat.emptyState")}
                </div>
              )}
              {messages.map((m) =>
                m.role === "user" ? (
                  <UserBubble key={m.id} text={m.text} />
                ) : (
                  <AssistantMessage
                    key={m.id}
                    m={m}
                    animate={!revealedRef.current.has(m.id)}
                    onRevealed={() => revealedRef.current.add(m.id)}
                    onUndo={() => undo(m)}
                    onReapply={() => reapply(m)}
                  />
                ),
              )}
            </div>

            {/* Composer */}
            <div className="relative border-t border-white/[0.07] p-3">
              {/* @-mention dropdown: the run's specs the reviewer can embed as context. */}
              {mention && mentionMatches.length > 0 && (
                <div className="absolute inset-x-3 bottom-full mb-2 max-h-56 overflow-y-auto rounded-xl border border-white/[0.12] bg-[#16161f] py-1 shadow-[0_20px_50px_-15px_rgba(0,0,0,.8)]">
                  <div className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wide text-ink-dim">
                    {t("automation.chat.specs")}
                  </div>
                  {mentionMatches.map((s) => (
                    <button
                      key={s.id}
                      onMouseDown={(e) => {
                        e.preventDefault(); // keep textarea focus
                        insertMention(s.filename);
                      }}
                      className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12px] hover:bg-white/[0.06]"
                    >
                      <Sparkles size={12} className="shrink-0 text-violet-300" />
                      <span className="truncate font-mono text-ink-soft">{s.filename}</span>
                    </button>
                  ))}
                </div>
              )}
              {guidanceMode && (
                // Say it before they type, not after: the same box does two very
                // different things depending on whether a device is authoring
                // this case right now, and only one of them edits the spec.
                <div
                  className="mb-2 rounded-lg px-2.5 py-1.5 text-[11px] text-violet-200"
                  style={{ background: "rgba(139,92,246,.14)" }}
                >
                  {t("progress.authoring.chatIsGuidance")}
                </div>
              )}
              <div className="mb-2 flex flex-wrap gap-1.5">
                {quickActions.map((q) => (
                  <button
                    key={q}
                    disabled={busy}
                    onClick={() => send(q)}
                    className="rounded-full border border-white/[0.1] bg-white/[0.03] px-2.5 py-1 text-[11px] text-ink-soft hover:bg-white/[0.08] disabled:opacity-40"
                  >
                    {q}
                  </button>
                ))}
              </div>
              <div className="flex items-end gap-2 rounded-xl border border-white/[0.16] bg-white/[0.07] p-2">
                <textarea
                  ref={textareaRef}
                  value={input}
                  onChange={(e) => {
                    setInput(e.target.value);
                    detectMention(e.target.value, e.target.selectionStart ?? e.target.value.length);
                  }}
                  onKeyDown={(e) => {
                    if (mention && mentionMatches.length > 0) {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        insertMention(mentionMatches[0].filename);
                        return;
                      }
                      if (e.key === "Escape") {
                        e.preventDefault();
                        e.stopPropagation(); // don't let the global Escape close the drawer
                        setMention(null);
                        return;
                      }
                    }
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      send(input);
                    }
                  }}
                  rows={1}
                  placeholder={t("automation.chat.placeholder")}
                  className="max-h-32 flex-1 resize-none bg-transparent px-1 text-[13.5px] text-ink outline-none placeholder:text-ink-dim"
                />
                <button
                  onClick={() => send(input)}
                  disabled={!input.trim() || busy}
                  className="accent-gradient flex h-8 w-8 shrink-0 items-center justify-center rounded-lg disabled:opacity-40"
                  title={t("automation.chat.send")}
                >
                  <Send size={15} color="#fff" />
                </button>
              </div>
            </div>
          </motion.aside>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}

function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="accent-gradient max-w-[85%] rounded-[14px_14px_5px_14px] px-3 py-2 text-[12.5px] text-white">
        {text}
      </div>
    </div>
  );
}

function AssistantMessage({
  m,
  animate,
  onRevealed,
  onUndo,
  onReapply,
}: {
  m: ChatMessage;
  animate: boolean;
  onRevealed: () => void;
  onUndo: () => void;
  onReapply: () => void;
}) {
  const { t } = useTranslation("pipeline");
  const { shown, done } = useTypewriter(m.thinking ? "" : m.text, animate);
  useEffect(() => {
    if (done && m.text) onRevealed();
  }, [done, m.text, onRevealed]);
  return (
    <div className="flex gap-2.5">
      <span className="accent-gradient mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg">
        <Sparkles size={13} color="#fff" />
      </span>
      <div className="min-w-0 flex-1 space-y-2">
        {m.thinking ? (
          <span className="inline-flex gap-1 text-ink-dim" aria-label={t("automation.chat.thinking")}>
            <Dot /> <Dot /> <Dot />
          </span>
        ) : m.error ? (
          <div className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-[12px] text-danger">
            {m.error}
          </div>
        ) : (
          <>
            <div className="whitespace-pre-wrap text-[13px] leading-relaxed text-ink">
              {shown}
              {!done && <span className="caret-blink">▍</span>}
            </div>
            {done && m.prevCode != null && m.code != null && (
              <>
                <DiffSnippet prev={m.prevCode} next={m.code} />
                {m.reverted ? (
                  <RevertedCard onReapply={onReapply} />
                ) : (
                  <AppliedCard prev={m.prevCode} next={m.code} onUndo={onUndo} />
                )}
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function Dot() {
  return <span className="inline-block h-1.5 w-1.5 rounded-full bg-ink-dim" style={{ animation: "var(--animate-think)" }} />;
}

/** Compact red/green line diff of what the edit changed. */
function DiffSnippet({ prev, next }: { prev: string; next: string }) {
  const { changed, removed } = useMemo(() => diffLines(prev, next), [prev, next]);
  const nextLines = next.split("\n");
  // Only real, non-blank changes — a no-op edit (e.g. "Ping") must not render a
  // stray empty "-"/"+" line.
  const added = nextLines.filter((_, i) => changed.has(i) && nextLines[i].trim() !== "").slice(0, 8);
  const gone = removed.filter((l) => l.trim() !== "").slice(0, 4);
  if (added.length === 0 && gone.length === 0) return null;
  return (
    <div className="overflow-x-auto rounded-lg border border-white/[0.08] bg-black/30 p-2 font-mono text-[11px] leading-relaxed">
      {gone.map((l, i) => (
        <div key={`r${i}`} style={{ color: "#fb7185", background: "rgba(251,113,133,.09)" }}>
          - {l}
        </div>
      ))}
      {added.map((l, i) => (
        <div key={`a${i}`} style={{ color: "#6ee7b7", background: "rgba(16,185,129,.1)" }}>
          + {l}
        </div>
      ))}
    </div>
  );
}

function AppliedCard({ prev, next, onUndo }: { prev: string; next: string; onUndo: () => void }) {
  const { t } = useTranslation("pipeline");
  const { count } = useMemo(() => diffLines(prev, next), [prev, next]);
  return (
    <div className="flex items-center gap-2 rounded-lg border border-success/30 bg-success/10 px-3 py-2 text-[12px]">
      <span className="text-success">
        {count === 0 ? t("automation.chat.appliedNoChanges") : t("automation.chat.appliedLines", { count })}
      </span>
      {count > 0 && (
        <button onClick={onUndo} className="ml-auto flex items-center gap-1 text-ink-soft hover:text-ink" title={t("automation.chat.undo")}>
          <Undo2 size={13} /> {t("automation.chat.undo")}
        </button>
      )}
    </div>
  );
}

function RevertedCard({ onReapply }: { onReapply: () => void }) {
  const { t } = useTranslation("pipeline");
  return (
    <div className="flex items-center gap-2 rounded-lg border border-white/[0.1] bg-white/[0.03] px-3 py-2 text-[12px] text-ink-dim">
      {t("automation.chat.editReverted")}
      <button onClick={onReapply} className="ml-auto text-ink-soft hover:text-ink">
        {t("automation.chat.reapply")}
      </button>
    </div>
  );
}
