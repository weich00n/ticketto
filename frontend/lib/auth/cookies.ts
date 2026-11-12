import type { NextResponse } from "next/server";
import { authCookieNames } from "./config";

type Tokens = {
  accessToken?: string;
  idToken?: string;
  refreshToken?: string;
};

// Matches Cognito's default refresh-token validity. Access and ID tokens expire
// sooner, but their cookies are kept so the server can see the session and
// refresh it instead of sending the user back to the sign-in page.
const sessionMaxAge = 60 * 60 * 24 * 30;

const cookieOptions = (maxAge?: number) => ({
  httpOnly: true,
  secure: process.env.NODE_ENV === "production",
  sameSite: "lax" as const,
  path: "/",
  ...(maxAge ? { maxAge } : {}),
});

export function setAuthCookies(response: NextResponse, tokens: Tokens) {
  if (tokens.accessToken)
    response.cookies.set(
      authCookieNames.accessToken,
      tokens.accessToken,
      cookieOptions(sessionMaxAge),
    );
  if (tokens.idToken)
    response.cookies.set(
      authCookieNames.idToken,
      tokens.idToken,
      cookieOptions(sessionMaxAge),
    );
  if (tokens.refreshToken)
    response.cookies.set(
      authCookieNames.refreshToken,
      tokens.refreshToken,
      cookieOptions(sessionMaxAge),
    );
}

export function clearAuthCookies(response: NextResponse) {
  Object.values(authCookieNames).forEach((name) =>
    response.cookies.set(name, "", { ...cookieOptions(), maxAge: 0 }),
  );
}
