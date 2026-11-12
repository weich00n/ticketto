import { cognitoError, resendConfirmationCode } from "@/lib/auth/cognito";
import { email } from "@/lib/auth/validation";

export async function POST(request: Request) {
  try {
    const body = await request.json();
    await resendConfirmationCode(email(body.email));
    return Response.json({ message: "A new verification code has been sent." });
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    if (error instanceof SyntaxError || message.startsWith("Enter a valid"))
      return Response.json({ message }, { status: 400 });
    const result = cognitoError(error);
    return Response.json(
      { message: result.message },
      { status: result.status },
    );
  }
}
