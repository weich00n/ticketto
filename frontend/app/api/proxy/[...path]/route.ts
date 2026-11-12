import { apiGatewayUrl, authCookieNames } from "@/lib/auth/config";
import { setAuthCookies } from "@/lib/auth/cookies";
import { refreshTokens } from "@/lib/auth/session";
import { isSafeProxyPath } from "@/lib/api/proxy-path";
import { isExpired } from "@/lib/auth/tokens";
import { NextRequest, NextResponse } from "next/server";

// Only these browser headers reach the gateway. Identity headers such as
// X-User-Sub are set by the gateway from the token, never by the browser.
const forwardedHeaders = ["accept", "content-type", "idempotency-key"];
const correlationHeader = "x-correlation-id";

// API Gateway stops waiting after 30 seconds; give up just before it does.
const upstreamTimeoutMs = 29_000;

const errorResponse = (
  status: number,
  code: string,
  message: string,
  correlationId: string,
) =>
  Response.json(
    { error: { code, message }, correlation_id: correlationId },
    { status, headers: { [correlationHeader]: correlationId } },
  );

const responseHeaders = (response: Response, correlationId: string) => {
  const headers = new Headers({ [correlationHeader]: correlationId });
  // Event images are served with a long cache lifetime and a version in their URL.
  for (const name of ["content-type", "cache-control"]) {
    const value = response.headers.get(name);
    if (value) headers.set(name, value);
  }
  return headers;
};

async function forward(
  request: NextRequest,
  target: URL,
  accessToken: string | undefined,
  body: ArrayBuffer | undefined,
  correlationId: string,
) {
  const headers = new Headers({ [correlationHeader]: correlationId });
  // Without a token the gateway decides whether the route is public.
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  forwardedHeaders.forEach((header) => {
    const value = request.headers.get(header);
    if (value) headers.set(header, value);
  });
  return fetch(target, {
    method: request.method,
    headers,
    body,
    cache: "no-store",
    signal: AbortSignal.timeout(upstreamTimeoutMs),
  });
}

async function handle(
  request: NextRequest,
  context: RouteContext<"/api/proxy/[...path]">,
) {
  const correlationId =
    request.headers.get(correlationHeader) || crypto.randomUUID();
  const gateway = apiGatewayUrl();
  const { path } = await context.params;
  if (!gateway)
    return errorResponse(
      503,
      "GATEWAY_NOT_CONFIGURED",
      "API gateway is not configured.",
      correlationId,
    );

  if (!isSafeProxyPath(path))
    return errorResponse(
      400,
      "INVALID_PATH",
      "That request path is not allowed.",
      correlationId,
    );

  const refreshToken = request.cookies.get(authCookieNames.refreshToken)?.value;
  const idToken = request.cookies.get(authCookieNames.idToken)?.value;
  const renew = async () => {
    if (!refreshToken) return undefined;
    try {
      return await refreshTokens(refreshToken, idToken);
    } catch {
      // Continue without a token; protected routes answer 401 and the caller can sign in again.
      return undefined;
    }
  };

  let accessToken = request.cookies.get(authCookieNames.accessToken)?.value;
  let refreshed: Awaited<ReturnType<typeof refreshTokens>> | undefined;
  if (refreshToken && (!accessToken || isExpired(accessToken))) {
    refreshed = await renew();
    accessToken = refreshed?.accessToken;
  }

  const target = new URL(path.join("/"), `${gateway}/`);
  target.search = request.nextUrl.search;
  const body = ["GET", "HEAD"].includes(request.method)
    ? undefined
    : await request.arrayBuffer();

  let upstream: Response;
  try {
    upstream = await forward(request, target, accessToken, body, correlationId);
    if (upstream.status === 401 && !refreshed && refreshToken) {
      refreshed = await renew();
      if (refreshed)
        upstream = await forward(
          request,
          target,
          refreshed.accessToken,
          body,
          correlationId,
        );
    }
  } catch (error) {
    if (error instanceof DOMException && error.name === "TimeoutError")
      return errorResponse(
        504,
        "GATEWAY_TIMEOUT",
        "The server took too long to respond. Please try again.",
        correlationId,
      );
    return errorResponse(
      502,
      "GATEWAY_UNREACHABLE",
      "We could not reach the server. Please try again.",
      correlationId,
    );
  }

  const response = new NextResponse(upstream.body, {
    status: upstream.status,
    headers: responseHeaders(
      upstream,
      upstream.headers.get(correlationHeader) || correlationId,
    ),
  });
  if (refreshed) setAuthCookies(response, refreshed);
  return response;
}

export {
  handle as GET,
  handle as POST,
  handle as PUT,
  handle as PATCH,
  handle as DELETE,
};
