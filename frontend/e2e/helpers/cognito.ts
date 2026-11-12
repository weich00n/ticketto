import {
  AdminCreateUserCommand,
  AdminDeleteUserCommand,
  AdminSetUserPasswordCommand,
  CognitoIdentityProviderClient,
} from "@aws-sdk/client-cognito-identity-provider";
import { randomBytes } from "node:crypto";

// Test accounts are made with the admin API, because signing up needs an
// emailed code. No email is sent, and the account is deleted after the test.
export type TestUser = { email: string; password: string; name: string };

const poolId = () => {
  const id = process.env.COGNITO_USER_POOL_ID;
  if (!id)
    throw new Error(
      "COGNITO_USER_POOL_ID is not set. Put it in .env.local and run with AWS_PROFILE set, e.g. AWS_PROFILE=ticketto npm run e2e.",
    );
  return id;
};

const client = () =>
  new CognitoIdentityProviderClient({
    region: process.env.AWS_REGION ?? "us-east-1",
  });

export async function createTestUser(role: string): Promise<TestUser> {
  const suffix = randomBytes(4).toString("hex");
  const user = {
    email: `e2e-${role}-${suffix}@example.com`,
    // Meets the pool's password rules: length, upper, lower, digit and symbol.
    password: `Aa1!${randomBytes(8).toString("hex")}`,
    name: `E2E ${role} ${suffix}`,
  };
  const cognito = client();
  await cognito.send(
    new AdminCreateUserCommand({
      UserPoolId: poolId(),
      Username: user.email,
      MessageAction: "SUPPRESS",
      UserAttributes: [
        { Name: "email", Value: user.email },
        { Name: "email_verified", Value: "true" },
        { Name: "name", Value: user.name },
      ],
    }),
  );
  await cognito.send(
    new AdminSetUserPasswordCommand({
      UserPoolId: poolId(),
      Username: user.email,
      Password: user.password,
      Permanent: true,
    }),
  );
  return user;
}

export async function deleteTestUser(user: TestUser) {
  await client().send(
    new AdminDeleteUserCommand({ UserPoolId: poolId(), Username: user.email }),
  );
}
