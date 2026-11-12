import type { Metadata } from "next";
import { AuthForm } from "@/features/auth/components/auth-form";

export const metadata: Metadata = { title: "Create your account" };

export default function SignUpPage() {
  return <AuthForm mode="signup" />;
}
