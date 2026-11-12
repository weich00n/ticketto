import { authCookieNames } from "@/lib/auth/config";
import { clearAuthCookies } from "@/lib/auth/cookies";
import { globalSignOut } from "@/lib/auth/cognito";
import { NextResponse } from "next/server";

export async function POST(request: Request) {
  const accessToken = request.headers
    .get("cookie")
    ?.match(new RegExp(`(?:^|; )${authCookieNames.accessToken}=([^;]+)`))?.[1];
  if (accessToken) {
    try {
      await globalSignOut(decodeURIComponent(accessToken));
    } catch {
      // Clearing local cookies still ends the current browser session.
    }
  }
  const response = NextResponse.json({ next: "/" });
  clearAuthCookies(response);
  return response;
}
