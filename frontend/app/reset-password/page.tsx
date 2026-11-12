import type { Metadata } from "next";
import { AuthForm } from "@/features/auth/components/auth-form";

export const metadata: Metadata = { title: "Choose a new password" };

export default async function ResetPasswordPage(
  props: PageProps<"/reset-password">,
) {
  const { email } = await props.searchParams;
  return (
    <AuthForm
      mode="reset"
      confirmationEmail={typeof email === "string" ? email : ""}
    />
  );
}
