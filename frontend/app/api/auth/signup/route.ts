import { cognitoError, signUp } from "@/lib/auth/cognito";
import { email, password, requiredString } from "@/lib/auth/validation";

export async function POST(request: Request) {
  try {
    const body = await request.json();
    await signUp(
      email(body.email),
      password(body.password),
      requiredString(body.name, "Name"),
    );
    return Response.json(
      { next: "/confirm", email: String(body.email).trim().toLowerCase() },
      { status: 201 },
    );
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    if (
      error instanceof SyntaxError ||
      message.endsWith("required.") ||
      message.startsWith("Enter a valid") ||
      message.startsWith("Password must")
    )
      return Response.json({ message }, { status: 400 });
    const result = cognitoError(error);
    return Response.json(
      { message: result.message },
      { status: result.status },
    );
  }
}
