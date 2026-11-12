import { organiserGroup } from "./config";

export type TokenClaims = {
  exp?: number;
  sub?: string;
  email?: string;
  name?: string;
  "cognito:username"?: string;
  "cognito:groups"?: string[];
};

export type SessionUser = {
  name: string;
  email: string;
  organiser: boolean;
};

// Reads claims without verifying the signature. Only use the result for UI and
// routing decisions; API Gateway verifies tokens before any service trusts them.
export function decodeJwt(token?: string): TokenClaims | null {
  const payload = token?.split(".")[1];
  if (!payload) return null;
  try {
    return JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
  } catch {
    return null;
  }
}

export function isExpired(token?: string, skewSeconds = 60) {
  const exp = decodeJwt(token)?.exp;
  return !exp || exp * 1000 <= Date.now() + skewSeconds * 1000;
}

export function sessionUser(idToken?: string): SessionUser | null {
  const claims = decodeJwt(idToken);
  if (!claims) return null;
  return {
    name: claims.name || claims.email || "",
    email: claims.email ?? "",
    organiser: claims["cognito:groups"]?.includes(organiserGroup) ?? false,
  };
}
