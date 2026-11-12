export const authCookieNames = {
  accessToken: "ticketto_access_token",
  idToken: "ticketto_id_token",
  refreshToken: "ticketto_refresh_token",
} as const;

export const organiserGroup = "organiser";

export const requiredAuthConfig = () => {
  const { AWS_REGION, COGNITO_CLIENT_ID, COGNITO_USER_POOL_ID } = process.env;

  if (!AWS_REGION || !COGNITO_CLIENT_ID || !COGNITO_USER_POOL_ID) {
    throw new Error("AUTH_CONFIGURATION_MISSING");
  }

  return { AWS_REGION, COGNITO_CLIENT_ID, COGNITO_USER_POOL_ID };
};

export const apiGatewayUrl = () =>
  process.env.API_GATEWAY_URL?.replace(/\/$/, "");
