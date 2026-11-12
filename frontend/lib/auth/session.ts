import { cookies } from "next/headers";
import { refreshSession } from "./cognito";
import { authCookieNames } from "./config";
import { decodeJwt, sessionUser } from "./tokens";

export async function refreshTokens(refreshToken: string, idToken?: string) {
  const username = decodeJwt(idToken)?.["cognito:username"];
  const result = await refreshSession(refreshToken, username);
  const tokens = result.AuthenticationResult;
  if (!tokens?.AccessToken || !tokens.IdToken)
    throw new Error("AUTHENTICATION_RESULT_INCOMPLETE");
  return { accessToken: tokens.AccessToken, idToken: tokens.IdToken };
}

export async function currentUser() {
  const store = await cookies();
  if (!store.get(authCookieNames.refreshToken)) return null;
  return sessionUser(store.get(authCookieNames.idToken)?.value);
}
