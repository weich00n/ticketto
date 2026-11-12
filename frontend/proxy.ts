import { authCookieNames } from "@/lib/auth/config";
import { setAuthCookies } from "@/lib/auth/cookies";
import { refreshTokens } from "@/lib/auth/session";
import { isExpired } from "@/lib/auth/tokens";
import { NextResponse, type NextRequest } from "next/server";

function redirectToLogin(request: NextRequest) {
  const loginUrl = new URL("/login", request.url);
  loginUrl.searchParams.set(
    "next",
    `${request.nextUrl.pathname}${request.nextUrl.search}`,
  );
  return NextResponse.redirect(loginUrl);
}

export async function proxy(request: NextRequest) {
  const accessToken = request.cookies.get(authCookieNames.accessToken)?.value;
  if (accessToken && !isExpired(accessToken)) return NextResponse.next();

  const refreshToken = request.cookies.get(authCookieNames.refreshToken)?.value;
  if (!refreshToken) return redirectToLogin(request);
  try {
    const tokens = await refreshTokens(
      refreshToken,
      request.cookies.get(authCookieNames.idToken)?.value,
    );
    // Update the incoming cookies too, so this render already sees the new tokens.
    request.cookies.set(authCookieNames.accessToken, tokens.accessToken);
    request.cookies.set(authCookieNames.idToken, tokens.idToken);
    const response = NextResponse.next({
      request: { headers: request.headers },
    });
    setAuthCookies(response, tokens);
    return response;
  } catch {
    return redirectToLogin(request);
  }
}

export const config = {
  matcher: ["/tickets/:path*", "/checkout/:path*", "/organizer/:path*"],
};
