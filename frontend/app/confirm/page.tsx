import type { Metadata } from "next";
import { AuthForm } from "@/features/auth/components/auth-form";

export const metadata: Metadata = { title: "Verify your email" };

export default async function ConfirmPage(props: PageProps<"/confirm">) {
  const { email } = await props.searchParams;
  return (
    <AuthForm
      mode="confirm"
      confirmationEmail={typeof email === "string" ? email : ""}
    />
  );
}
