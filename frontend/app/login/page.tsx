import type { Metadata } from "next";
import { AuthForm } from "@/features/auth/components/auth-form";
import { safeRedirectPath } from "@/lib/auth/validation";

export const metadata: Metadata = { title: "Sign in" };

export default async function LoginPage(props: PageProps<"/login">) {
  const { next, reset } = await props.searchParams;
  return (
    <AuthForm
      mode="login"
      redirectTo={safeRedirectPath(next)}
      notice={reset ? "Your password has been changed. Sign in with it." : ""}
    />
  );
}
