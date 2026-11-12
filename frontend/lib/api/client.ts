// Browser-side calls to the backend services. Requests go through the
// /api/proxy route, which attaches the signed-in user's access token.

// Most services send { error: { code, message } }; Checkout sends
// { message, error: "<detail>" } and answers some 404s with only { message }.
type ErrorBody = {
  error?:
    | string
    | { code?: string; message?: string; details?: Record<string, string> };
  message?: string;
};

export type Page<T> = {
  items: T[];
  page: number;
  page_size: number;
  total_items: number;
};

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly code?: string,
    readonly correlationId?: string,
    // What was wrong, field by field, when the service says so.
    readonly details?: Record<string, string>,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type ApiOptions = Omit<RequestInit, "body"> & {
  json?: unknown;
  // Send a file as the request body, with its own type as the content type.
  file?: Blob;
  // Send the visitor to the sign-in page when the request needs a session.
  redirectOnUnauthorized?: boolean;
};

const fallbackMessage = (status: number) =>
  status >= 500
    ? "Something went wrong on our side. Please try again."
    : "The request could not be completed.";

function redirectToLogin() {
  const next = `${window.location.pathname}${window.location.search}`;
  // A full page load, so the server renders the sign-in page with fresh cookies.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination -- not a component; no router here.
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
}

export async function api<T>(
  path: string,
  {
    json,
    file,
    redirectOnUnauthorized = true,
    headers,
    ...init
  }: ApiOptions = {},
): Promise<T> {
  const requestHeaders = new Headers(headers);
  requestHeaders.set("accept", "application/json");
  requestHeaders.set("x-correlation-id", crypto.randomUUID());
  if (json !== undefined)
    requestHeaders.set("content-type", "application/json");
  if (file) requestHeaders.set("content-type", file.type);

  let response: Response;
  try {
    response = await fetch(`/api/proxy${path}`, {
      ...init,
      headers: requestHeaders,
      body: file ?? (json === undefined ? undefined : JSON.stringify(json)),
    });
  } catch {
    throw new ApiError(0, "We could not reach the server. Please try again.");
  }

  const correlationId = response.headers.get("x-correlation-id") ?? undefined;
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ErrorBody;
    if (response.status === 401 && redirectOnUnauthorized) redirectToLogin();
    const error =
      typeof body.error === "string" ? { message: body.error } : body.error;
    throw new ApiError(
      response.status,
      error?.message ?? body.message ?? fallbackMessage(response.status),
      error?.code,
      correlationId,
      error && "details" in error ? error.details : undefined,
    );
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
