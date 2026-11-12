import { cognitoError, forgotPassword } from "@/lib/auth/cognito";
import { email } from "@/lib/auth/validation";

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const address = email(body.email);
    try {
      await forgotPassword(address);
    } catch (error) {
      // An unknown address gets the same reply as a known one, so this form
      // cannot be used to find out who has an account.
      if (!(error instanceof Error && error.name === "UserNotFoundException"))
        throw error;
    }
    return Response.json({ next: "/reset-password", email: address });
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    if (error instanceof SyntaxError || message.startsWith("Enter a valid"))
      return Response.json({ message }, { status: 400 });
    // Cognito will not reset the password of an account whose email was never confirmed.
    if (error instanceof Error && error.name === "InvalidParameterException")
      return Response.json(
        { message: "Confirm your email before resetting your password." },
        { status: 400 },
      );
    const result = cognitoError(error);
    return Response.json(
      { message: result.message },
      { status: result.status },
    );
  }
}
