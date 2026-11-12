import { cognitoError, confirmSignUp } from "@/lib/auth/cognito";
import { email, requiredString } from "@/lib/auth/validation";

export async function POST(request: Request) {
  try {
    const body = await request.json();
    await confirmSignUp(
      email(body.email),
      requiredString(body.code, "Verification code"),
    );
    return Response.json({ next: "/login" });
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    if (
      error instanceof SyntaxError ||
      message.endsWith("required.") ||
      message.startsWith("Enter a valid")
    )
      return Response.json({ message }, { status: 400 });
    const result = cognitoError(error);
    return Response.json(
      { message: result.message },
      { status: result.status },
    );
  }
}
