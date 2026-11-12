import { apiGatewayUrl } from "@/lib/auth/config";
import { setAuthCookies } from "@/lib/auth/cookies";
import { authenticate, cognitoError } from "@/lib/auth/cognito";
import { email, password } from "@/lib/auth/validation";
import { NextResponse } from "next/server";

async function createUserProfile(accessToken: string) {
  const baseUrl = apiGatewayUrl();
  if (!baseUrl) return;
  const response = await fetch(`${baseUrl}/api/v1/users/me`, {
    method: "POST",
    headers: { Authorization: `Bearer ${accessToken}` },
    cache: "no-store",
  });
  if (!response.ok) throw new Error("PROFILE_INITIALIZATION_FAILED");
}

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const result = await authenticate(
      email(body.email),
      password(body.password),
    );
    const tokens = result.AuthenticationResult;
    if (!tokens?.AccessToken || !tokens.IdToken || !tokens.RefreshToken)
      throw new Error("AUTHENTICATION_RESULT_INCOMPLETE");

    await createUserProfile(tokens.AccessToken);
    const response = NextResponse.json({ next: "/" });
    setAuthCookies(response, {
      accessToken: tokens.AccessToken,
      idToken: tokens.IdToken,
      refreshToken: tokens.RefreshToken,
    });
    return response;
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    if (
      error instanceof SyntaxError ||
      message.endsWith("required.") ||
      message.startsWith("Enter a valid") ||
      message.startsWith("Password must")
    )
      return Response.json({ message }, { status: 400 });
    if (message === "PROFILE_INITIALIZATION_FAILED")
      return Response.json(
        { message: "Your account could not be initialized. Please try again." },
        { status: 503 },
      );
    const result = cognitoError(error);
    return Response.json(
      { message: result.message },
      { status: result.status },
    );
  }
}
