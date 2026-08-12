/**
 * Structured automation logging (doc §9 `logging/`).
 *
 * Emits one JSON object per line to stdout/stderr so run output stays greppable and
 * machine-parseable by Q-Agent's execution analyzer, while remaining readable in a
 * terminal. Nothing here is application-specific.
 */

/** Severity levels, ordered least → most severe. */
export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

const LEVEL_ORDER: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 };

/** A single structured log record as emitted on stdout/stderr. */
export interface LogRecord {
  /** ISO-8601 timestamp of the record. */
  ts: string;
  level: LogLevel;
  /** Dot-joined logger scope, e.g. `"auth.login"`. */
  scope: string;
  message: string;
  /** Arbitrary structured context merged from the logger's bindings and the call site. */
  context?: Record<string, unknown>;
}

/** Options for {@link createLogger}. */
export interface LoggerOptions {
  /** Minimum level to emit. Defaults to `QAGENT_LOG_LEVEL` or `'info'`. */
  level?: LogLevel;
  /** Context merged into every record this logger emits. */
  context?: Record<string, unknown>;
  /** Sink for rendered lines. Defaults to `console.log` / `console.error` by level. */
  sink?: (record: LogRecord, line: string) => void;
}

/** A scoped structured logger. */
export interface AutomationLogger {
  readonly scope: string;
  readonly level: LogLevel;
  debug(message: string, context?: Record<string, unknown>): void;
  info(message: string, context?: Record<string, unknown>): void;
  warn(message: string, context?: Record<string, unknown>): void;
  error(message: string, context?: Record<string, unknown>): void;
  log(level: LogLevel, message: string, context?: Record<string, unknown>): void;
  /** A logger nested under this one, e.g. `logger.child('login')`. */
  child(scope: string, context?: Record<string, unknown>): AutomationLogger;
  /** Time an async operation, logging start/finish (or failure) and re-throwing. */
  step<T>(name: string, fn: () => Promise<T>, context?: Record<string, unknown>): Promise<T>;
}

function resolveDefaultLevel(): LogLevel {
  const raw = (typeof process !== 'undefined' ? process.env?.QAGENT_LOG_LEVEL : undefined) || '';
  const candidate = raw.trim().toLowerCase();
  return candidate in LEVEL_ORDER ? (candidate as LogLevel) : 'info';
}

function defaultSink(record: LogRecord, line: string): void {
  if (record.level === 'error' || record.level === 'warn') console.error(line);
  else console.log(line);
}

/**
 * Create a scoped structured logger.
 *
 * @param scope Logical scope name, e.g. `"api"` or `"evidence"`.
 * @param options Level, bound context and sink overrides.
 */
export function createLogger(scope: string, options: LoggerOptions = {}): AutomationLogger {
  const level = options.level ?? resolveDefaultLevel();
  const bound = options.context ?? {};
  const sink = options.sink ?? defaultSink;
  const threshold = LEVEL_ORDER[level];

  const emit = (recordLevel: LogLevel, message: string, context?: Record<string, unknown>): void => {
    if (LEVEL_ORDER[recordLevel] < threshold) return;
    const merged = { ...bound, ...(context ?? {}) };
    const record: LogRecord = {
      ts: new Date().toISOString(),
      level: recordLevel,
      scope,
      message,
      ...(Object.keys(merged).length ? { context: merged } : {}),
    };
    let line: string;
    try {
      line = JSON.stringify(record);
    } catch {
      line = JSON.stringify({ ts: record.ts, level: recordLevel, scope, message });
    }
    sink(record, line);
  };

  const logger: AutomationLogger = {
    scope,
    level,
    debug: (message, context) => emit('debug', message, context),
    info: (message, context) => emit('info', message, context),
    warn: (message, context) => emit('warn', message, context),
    error: (message, context) => emit('error', message, context),
    log: (recordLevel, message, context) => emit(recordLevel, message, context),
    child: (childScope, context) =>
      createLogger(`${scope}.${childScope}`, { level, context: { ...bound, ...(context ?? {}) }, sink }),
    step: async (name, fn, context) => {
      const startedAt = Date.now();
      emit('debug', `${name} — start`, context);
      try {
        const result = await fn();
        emit('info', `${name} — ok`, { ...(context ?? {}), durationMs: Date.now() - startedAt });
        return result;
      } catch (error) {
        emit('error', `${name} — failed`, {
          ...(context ?? {}),
          durationMs: Date.now() - startedAt,
          error: error instanceof Error ? error.message : String(error),
        });
        throw error;
      }
    },
  };
  return logger;
}

/** Package-level default logger (scope `"qagent"`). */
export const logger: AutomationLogger = createLogger('qagent');
