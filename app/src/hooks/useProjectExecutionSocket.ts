import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import type { ExecCaseStatus, ProgressEvent, ProjectExecutionOut } from "@/types/api";

/**
 * Live progress for a **project-scoped** execution (#800).
 *
 * ## Why this is not `useRunSocket`
 *
 * The run socket is owned by `RunLayout` / `RunSocketProvider`, which exist only
 * under `/runs/:runId/*` — the project Automation tab is not a run screen and
 * has no run to resolve, and per CLAUDE.md there is no "current run" fallback to
 * reach for. So this subscribes to the channel key the foundation slice (#796)
 * publishes a run-less execution on: `execution_service.channel_key` returns
 * `project:<automationProjectId>` when `run_id` is `NULL`, keyed on the repo id
 * rather than the project GUID precisely because the repo id is what the tab
 * already holds.
 *
 * `/ws/projects/{repoId}` (#808) is the route that serves that channel: it
 * subscribes to `project:<repoId>` and applies the same ownership check the run
 * socket applies, through the `AutomationProject` instead of the `Run`. Before
 * it existed the only endpoint was `/ws/runs/{id}`, whose check parses the
 * channel as an integer, so `project:<id>` was closed with 1008 wherever
 * `auth_required` was on and the bar moved only because {@link useProjectExecution}
 * polled. That poll is now a slow safety net, not the mechanism.
 *
 * Reconnects with the same bounded backoff as `useRunSocket`.
 *
 * @param repoId The `AutomationProject.id` whose channel to listen on, or null
 *   to subscribe to nothing.
 * @param executionId The execution whose cache entries the events apply to, or
 *   null when nothing has been started in this visit.
 */
export function useProjectExecutionSocket(
  repoId: number | null,
  executionId: number | null,
): void {
  const qc = useQueryClient();
  // Read through a ref so a new execution id does not tear down and rebuild the
  // socket — the channel is the repo's, and it outlives any one execution.
  const executionRef = useRef(executionId);
  executionRef.current = executionId;

  useEffect(() => {
    if (repoId == null) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let retry = 0;
    let timer: ReturnType<typeof setTimeout>;

    const connect = () => {
      if (closed) return;
      ws = new WebSocket(api.wsProjectUrl(repoId));
      ws.onopen = () => {
        retry = 0;
      };
      ws.onmessage = (msg) => {
        let evt: ProgressEvent;
        try {
          evt = JSON.parse(msg.data as string) as ProgressEvent;
        } catch {
          return;
        }
        if (!evt.event.startsWith("exec")) return;
        const id = executionRef.current;
        if (id == null) return;
        const key = queryKeys.projectExecution(id);

        // Optimistic, exactly as the run path does it: apply the event to the
        // cached row so the counters and the per-spec dot flip on arrival, then
        // let the invalidate reconcile against the server a moment later.
        if (evt.event === "exec.case.result") {
          const p = evt.payload as {
            specPath?: string;
            file?: string;
            status?: string;
            durationMs?: number;
          };
          // A project-scoped result's ONLY identity is its path (#796), so there
          // is no ticket/case fallback to match on here.
          const path = p.specPath ?? p.file ?? "";
          if (path) {
            qc.setQueryData<ProjectExecutionOut>(key, (old) => {
              if (!old?.results) return old;
              let hit = false;
              const results = old.results.map((r) => {
                if (hit || r.specPath !== path) return r;
                hit = true;
                return {
                  ...r,
                  status: (p.status as ExecCaseStatus) ?? r.status,
                  durationMs: p.durationMs ?? r.durationMs,
                };
              });
              return hit ? { ...old, results } : old;
            });
          }
        } else if (evt.event === "exec.progress" || evt.event === "exec.done") {
          const p = evt.payload as { passed?: number; failed?: number; progress?: number };
          qc.setQueryData<ProjectExecutionOut>(key, (old) =>
            old
              ? {
                  ...old,
                  passed: typeof p.passed === "number" ? p.passed : old.passed,
                  failed: typeof p.failed === "number" ? p.failed : old.failed,
                  progress:
                    evt.event === "exec.done"
                      ? 100
                      : typeof p.progress === "number"
                        ? p.progress
                        : old.progress,
                }
              : old,
          );
        }
        qc.invalidateQueries({ queryKey: key });
      };
      ws.onclose = () => {
        if (closed) return;
        retry += 1;
        timer = setTimeout(connect, Math.min(1000 * retry, 5000));
      };
      ws.onerror = () => ws?.close();
    };

    connect();
    return () => {
      closed = true;
      clearTimeout(timer);
      ws?.close();
    };
  }, [repoId, qc]);
}
