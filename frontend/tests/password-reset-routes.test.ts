import { beforeEach, describe, expect, it, vi } from "vitest";

const cognito = vi.hoisted(() => ({
  forgotPassword: vi.fn(),
  confirmForgotPassword: vi.fn(),
}));

vi.mock("@/lib/auth/cognito", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/auth/cognito")>()),
  ...cognito,
}));

import { POST as forgot } from "@/app/api/auth/forgot-password/route";
import { POST as reset } from "@/app/api/auth/reset-password/route";

const post = (body: unknown) =>
  new Request("http://localhost/api", {
    method: "POST",
    body: JSON.stringify(body),
  });

const cognitoFailure = (name: string) =>
  Object.assign(new Error(name), { name });

beforeEach(() => vi.resetAllMocks());

describe("forgot password", () => {
  it("asks Cognito for a code and points to the reset page", async () => {
    cognito.forgotPassword.mockResolvedValue({});
    const response = await forgot(post({ email: " Ann@Example.com " }));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      next: "/reset-password",
      email: "ann@example.com",
    });
    expect(cognito.forgotPassword).toHaveBeenCalledWith("ann@example.com");
  });

  it("answers the same for an address with no account", async () => {
    cognito.forgotPassword.mockRejectedValue(
      cognitoFailure("UserNotFoundException"),
    );
    const response = await forgot(post({ email: "nobody@example.com" }));
    expect(response.status).toBe(200);
  });

  it("tells an unconfirmed account to confirm its email first", async () => {
    cognito.forgotPassword.mockRejectedValue(
      cognitoFailure("InvalidParameterException"),
    );
    const response = await forgot(post({ email: "ann@example.com" }));
    expect(response.status).toBe(400);
    expect((await response.json()).message).toMatch(/Confirm your email/);
  });

  it("rejects a malformed address without calling Cognito", async () => {
    const response = await forgot(post({ email: "not-an-email" }));
    expect(response.status).toBe(400);
    expect(cognito.forgotPassword).not.toHaveBeenCalled();
  });

  it("limits repeated requests", async () => {
    cognito.forgotPassword.mockRejectedValue(
      cognitoFailure("LimitExceededException"),
    );
    const response = await forgot(post({ email: "ann@example.com" }));
    expect(response.status).toBe(429);
  });
});

describe("reset password", () => {
  const body = {
    email: "ann@example.com",
    code: "123456",
    password: "a-new-password",
  };

  it("sets the new password and sends the user to sign in", async () => {
    cognito.confirmForgotPassword.mockResolvedValue({});
    const response = await reset(post(body));
    expect(await response.json()).toEqual({ next: "/login?reset=1" });
    expect(cognito.confirmForgotPassword).toHaveBeenCalledWith(
      "ann@example.com",
      "123456",
      "a-new-password",
    );
  });

  it("rejects a short password before calling Cognito", async () => {
    const response = await reset(post({ ...body, password: "short" }));
    expect(response.status).toBe(400);
    expect(cognito.confirmForgotPassword).not.toHaveBeenCalled();
  });

  it("reports a wrong or expired code", async () => {
    cognito.confirmForgotPassword.mockRejectedValue(
      cognitoFailure("CodeMismatchException"),
    );
    const response = await reset(post(body));
    expect(response.status).toBe(400);
    expect((await response.json()).message).toMatch(/incorrect/);
  });

  it("does not reveal that an address has no account", async () => {
    cognito.confirmForgotPassword.mockRejectedValue(
      cognitoFailure("UserNotFoundException"),
    );
    const response = await reset(post(body));
    expect(response.status).toBe(400);
    expect((await response.json()).message).toMatch(/incorrect/);
  });
});
