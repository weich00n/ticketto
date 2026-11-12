import { authCookieNames } from "@/lib/auth/config";
import { setAuthCookies } from "@/lib/auth/cookies";
import { cognitoError } from "@/lib/auth/cognito";
import { refreshTokens } from "@/lib/auth/session";
import { NextResponse, type NextRequest } from "next/server";

export async function POST(request: NextRequest) {
  const refreshToken = request.cookies.get(authCookieNames.refreshToken)?.value;
  if (!refreshToken)
    return Response.json({ message: "Sign in is required." }, { status: 401 });
  try {
    const tokens = await refreshTokens(
      refreshToken,
      request.cookies.get(authCookieNames.idToken)?.value,
    );
    const response = NextResponse.json({ ok: true });
    setAuthCookies(response, tokens);
    return response;
  } catch (error) {
    const result = cognitoError(error);
    return Response.json(
      { message: result.message },
      { status: result.status },
    );
  }
}
