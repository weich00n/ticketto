import { cognitoError, confirmForgotPassword } from "@/lib/auth/cognito";
import { email, password, requiredString } from "@/lib/auth/validation";

export async function POST(request: Request) {
  try {
    const body = await request.json();
    await confirmForgotPassword(
      email(body.email),
      requiredString(body.code, "Verification code"),
      password(body.password),
    );
    return Response.json({ next: "/login?reset=1" });
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    if (
      error instanceof SyntaxError ||
      message.endsWith("required.") ||
      message.startsWith("Enter a valid") ||
      message.startsWith("Password must")
    )
      return Response.json({ message }, { status: 400 });
    // Same answer as a wrong code, so an unknown address is not revealed.
    if (error instanceof Error && error.name === "UserNotFoundException")
      return Response.json(
        { message: "That verification code is incorrect." },
        { status: 400 },
      );
    const result = cognitoError(error);
    return Response.json(
      { message: result.message },
      { status: result.status },
    );
  }
}
