import {
  CognitoIdentityProviderClient,
  ConfirmForgotPasswordCommand,
  ConfirmSignUpCommand,
  ForgotPasswordCommand,
  GlobalSignOutCommand,
  InitiateAuthCommand,
  ResendConfirmationCodeCommand,
  SignUpCommand,
} from "@aws-sdk/client-cognito-identity-provider";
import { createHmac } from "node:crypto";
import { requiredAuthConfig } from "./config";

const client = () => {
  const { AWS_REGION } = requiredAuthConfig();
  return new CognitoIdentityProviderClient({
    region: AWS_REGION,
    endpoint: process.env.COGNITO_ENDPOINT || undefined,
  });
};

const secretHash = (username: string) => {
  const { COGNITO_CLIENT_ID } = requiredAuthConfig();
  const secret = process.env.COGNITO_CLIENT_SECRET;
  return secret
    ? createHmac("sha256", secret)
        .update(`${username}${COGNITO_CLIENT_ID}`)
        .digest("base64")
    : undefined;
};

// InitiateAuth takes the hash inside AuthParameters as SECRET_HASH, while the
// sign-up and confirmation commands take it as a top-level SecretHash field.
const authParameters = (username: string): Record<string, string> => {
  const hash = secretHash(username);
  return hash ? { SECRET_HASH: hash } : {};
};

export async function signUp(email: string, password: string, name: string) {
  const { COGNITO_CLIENT_ID } = requiredAuthConfig();
  return client().send(
    new SignUpCommand({
      ClientId: COGNITO_CLIENT_ID,
      Username: email,
      Password: password,
      UserAttributes: [
        { Name: "email", Value: email },
        { Name: "name", Value: name },
      ],
      SecretHash: secretHash(email),
    }),
  );
}

export async function confirmSignUp(email: string, code: string) {
  const { COGNITO_CLIENT_ID } = requiredAuthConfig();
  return client().send(
    new ConfirmSignUpCommand({
      ClientId: COGNITO_CLIENT_ID,
      Username: email,
      ConfirmationCode: code,
      SecretHash: secretHash(email),
    }),
  );
}

export async function resendConfirmationCode(email: string) {
  const { COGNITO_CLIENT_ID } = requiredAuthConfig();
  return client().send(
    new ResendConfirmationCodeCommand({
      ClientId: COGNITO_CLIENT_ID,
      Username: email,
      SecretHash: secretHash(email),
    }),
  );
}

// Sends a reset code to the account's verified email.
export async function forgotPassword(email: string) {
  const { COGNITO_CLIENT_ID } = requiredAuthConfig();
  return client().send(
    new ForgotPasswordCommand({
      ClientId: COGNITO_CLIENT_ID,
      Username: email,
      SecretHash: secretHash(email),
    }),
  );
}

export async function confirmForgotPassword(
  email: string,
  code: string,
  newPassword: string,
) {
  const { COGNITO_CLIENT_ID } = requiredAuthConfig();
  return client().send(
    new ConfirmForgotPasswordCommand({
      ClientId: COGNITO_CLIENT_ID,
      Username: email,
      ConfirmationCode: code,
      Password: newPassword,
      SecretHash: secretHash(email),
    }),
  );
}

export async function authenticate(email: string, password: string) {
  const { COGNITO_CLIENT_ID } = requiredAuthConfig();
  return client().send(
    new InitiateAuthCommand({
      AuthFlow: "USER_PASSWORD_AUTH",
      ClientId: COGNITO_CLIENT_ID,
      AuthParameters: {
        USERNAME: email,
        PASSWORD: password,
        ...authParameters(email),
      },
    }),
  );
}

// With email sign-in, Cognito's username is the user's sub rather than their
// email, so the secret hash must use the `cognito:username` token claim.
export async function refreshSession(refreshToken: string, username?: string) {
  const { COGNITO_CLIENT_ID } = requiredAuthConfig();
  return client().send(
    new InitiateAuthCommand({
      AuthFlow: "REFRESH_TOKEN_AUTH",
      ClientId: COGNITO_CLIENT_ID,
      AuthParameters: {
        REFRESH_TOKEN: refreshToken,
        ...(username ? authParameters(username) : {}),
      },
    }),
  );
}

export async function globalSignOut(accessToken: string) {
  return client().send(new GlobalSignOutCommand({ AccessToken: accessToken }));
}

export function cognitoError(error: unknown) {
  const name = error instanceof Error ? error.name : "UnknownError";
  // Cognito reports app-client misconfiguration (such as a missing secret hash)
  // as NotAuthorizedException, which must not reach users as a password error.
  if (
    error instanceof Error &&
    /SECRET_HASH|client secret|flow not enabled/i.test(error.message)
  ) {
    console.error(`Cognito configuration error: ${error.message}`);
    return {
      status: 500,
      message: "Authentication is temporarily unavailable.",
    };
  }
  const messages: Record<string, { status: number; message: string }> = {
    UserNotConfirmedException: {
      status: 403,
      message: "Confirm your email before signing in.",
    },
    NotAuthorizedException: {
      status: 401,
      message: "Incorrect email or password.",
    },
    UsernameExistsException: {
      status: 409,
      message: "An account already exists for this email.",
    },
    InvalidPasswordException: {
      status: 400,
      message: "Your password does not meet the required policy.",
    },
    CodeMismatchException: {
      status: 400,
      message: "That verification code is incorrect.",
    },
    ExpiredCodeException: {
      status: 400,
      message: "That verification code has expired.",
    },
    TooManyRequestsException: {
      status: 429,
      message: "Please wait before trying again.",
    },
    LimitExceededException: {
      status: 429,
      message: "Please wait before trying again.",
    },
  };
  if (messages[name]) return messages[name];
  console.error(
    `Unexpected Cognito error: ${name}`,
    error instanceof Error ? error.message : "",
  );
  return {
    status: 500,
    message: "Authentication is temporarily unavailable.",
  };
}
