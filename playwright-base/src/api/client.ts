/**
 * API client helpers (doc §9 `api/`).
 *
 * A thin, generic wrapper over Playwright's `APIRequestContext` so specs and page
 * objects can do setup/teardown and assertions over HTTP without repeating URL
 * joining, header merging and JSON parsing. It knows no endpoints — an automation
 * project's own API layer builds on this.
 */

import { request as playwrightRequest } from '../runtime';
import type { APIRequestContext, APIResponse } from '../runtime';
import { resolveUrl } from '../config/environment';
import { createLogger } from '../logging/logger';

const log = createLogger('api');

/** Per-call options. */
export interface ApiRequestOptions {
  /** Query parameters appended to the URL. */
  params?: Record<string, string | number | boolean>;
  /** Headers merged over the client's defaults. */
  headers?: Record<string, string>;
  /** JSON request body. Mutually exclusive with `data`/`form`. */
  json?: unknown;
  /** Raw request body (string/Buffer). */
  data?: string | Buffer;
  /** `application/x-www-form-urlencoded` body. */
  form?: Record<string, string | number | boolean>;
  /** `multipart/form-data` body. */
  multipart?: Record<string, string | number | boolean | { name: string; mimeType: string; buffer: Buffer }>;
  /** Per-call timeout, ms. */
  timeout?: number;
  /** When false, a non-2xx response still resolves rather than throwing. Default false. */
  failOnStatusCode?: boolean;
}

/** A response plus its parsed JSON body. */
export interface ApiResult<T = unknown> {
  status: number;
  ok: boolean;
  headers: Record<string, string>;
  /** Parsed JSON body, or `null` when the body was empty/not JSON. */
  body: T | null;
  /** Raw response text. */
  text: string;
  /** The underlying Playwright response, for anything not covered above. */
  response: APIResponse;
}

/** A configured API client. */
export interface ApiClient {
  readonly baseUrl: string;
  get<T = unknown>(path: string, options?: ApiRequestOptions): Promise<ApiResult<T>>;
  post<T = unknown>(path: string, options?: ApiRequestOptions): Promise<ApiResult<T>>;
  put<T = unknown>(path: string, options?: ApiRequestOptions): Promise<ApiResult<T>>;
  patch<T = unknown>(path: string, options?: ApiRequestOptions): Promise<ApiResult<T>>;
  /** `delete` is a reserved word in some positions, hence the name. */
  del<T = unknown>(path: string, options?: ApiRequestOptions): Promise<ApiResult<T>>;
  /** Escape hatch: any method, returning the raw Playwright response. */
  raw(method: string, path: string, options?: ApiRequestOptions): Promise<APIResponse>;
  /** Set/replace the bearer token used for subsequent calls. */
  setBearerToken(token: string): void;
  /** The underlying `APIRequestContext`. */
  readonly context: APIRequestContext;
}

/** Options for {@link createApiClient}. */
export interface ApiClientOptions {
  /** An existing `APIRequestContext` — normally `request` from a Playwright fixture. */
  context: APIRequestContext;
  /** Base URL prefixed onto relative paths. */
  baseUrl?: string;
  /** Headers sent on every call. */
  defaultHeaders?: Record<string, string>;
  /** Convenience for `Authorization: Bearer <token>`. */
  bearerToken?: string;
}

function toQuery(params?: Record<string, string | number | boolean>): string {
  if (!params) return '';
  const entries = Object.entries(params).map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return entries.length ? `?${entries.join('&')}` : '';
}

/** Create an {@link ApiClient} over an existing `APIRequestContext`. */
export function createApiClient(options: ApiClientOptions): ApiClient {
  const { context, baseUrl = '', defaultHeaders = {} } = options;
  const headers: Record<string, string> = { ...defaultHeaders };
  if (options.bearerToken) headers.Authorization = `Bearer ${options.bearerToken}`;

  const raw = async (method: string, path: string, opts: ApiRequestOptions = {}): Promise<APIResponse> => {
    const url = `${resolveUrl(baseUrl, path)}${toQuery(opts.params)}`;
    const started = Date.now();
    const response = await context.fetch(url, {
      method,
      headers: { ...headers, ...(opts.headers ?? {}) },
      ...(opts.json !== undefined ? { data: opts.json } : {}),
      ...(opts.data !== undefined ? { data: opts.data } : {}),
      ...(opts.form ? { form: opts.form } : {}),
      ...(opts.multipart ? { multipart: opts.multipart as never } : {}),
      ...(opts.timeout != null ? { timeout: opts.timeout } : {}),
      failOnStatusCode: opts.failOnStatusCode ?? false,
    });
    log.debug(`${method} ${url}`, { status: response.status(), durationMs: Date.now() - started });
    return response;
  };

  const call = async <T>(method: string, path: string, opts?: ApiRequestOptions): Promise<ApiResult<T>> => {
    const response = await raw(method, path, opts);
    const text = await response.text().catch(() => '');
    let body: T | null = null;
    if (text) {
      try {
        body = JSON.parse(text) as T;
      } catch {
        body = null;
      }
    }
    return { status: response.status(), ok: response.ok(), headers: response.headers(), body, text, response };
  };

  return {
    baseUrl,
    context,
    get: (path, opts) => call('GET', path, opts),
    post: (path, opts) => call('POST', path, opts),
    put: (path, opts) => call('PUT', path, opts),
    patch: (path, opts) => call('PATCH', path, opts),
    del: (path, opts) => call('DELETE', path, opts),
    raw,
    setBearerToken: (token: string) => {
      headers.Authorization = `Bearer ${token}`;
    },
  };
}

/**
 * Create a standalone {@link ApiClient} with its own `APIRequestContext` — for use
 * outside a test (global setup/teardown, data seeding).
 *
 * The caller owns disposal: `await client.context.dispose()`.
 */
export async function createStandaloneApiClient(
  options: Omit<ApiClientOptions, 'context'> & { storageStatePath?: string },
): Promise<ApiClient> {
  const context = await playwrightRequest.newContext({
    ...(options.baseUrl ? { baseURL: options.baseUrl } : {}),
    ...(options.storageStatePath ? { storageState: options.storageStatePath } : {}),
    ...(options.defaultHeaders ? { extraHTTPHeaders: options.defaultHeaders } : {}),
  });
  return createApiClient({ ...options, context });
}
