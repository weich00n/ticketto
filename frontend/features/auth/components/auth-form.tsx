"use client";

import { ArrowLeft, Eye, EyeOff } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useId, useRef, useState } from "react";

type FormMode = "login" | "signup" | "confirm" | "forgot" | "reset";

const content = {
  login: {
    label: "WELCOME BACK",
    title: "Your next experience awaits.",
    submit: "Sign in",
    endpoint: "/api/auth/login",
  },
  signup: {
    label: "CREATE YOUR ACCOUNT",
    title: "Start finding remarkable moments.",
    submit: "Create account",
    endpoint: "/api/auth/signup",
  },
  confirm: {
    label: "VERIFY YOUR EMAIL",
    title: "One last step before you begin.",
    submit: "Confirm email",
    endpoint: "/api/auth/confirm",
  },
  forgot: {
    label: "RESET YOUR PASSWORD",
    title: "We'll email you a code.",
    submit: "Send reset code",
    endpoint: "/api/auth/forgot-password",
  },
  reset: {
    label: "CHOOSE A NEW PASSWORD",
    title: "Enter the code we emailed you.",
    submit: "Reset password",
    endpoint: "/api/auth/reset-password",
  },
};

export function AuthForm({
  mode,
  confirmationEmail = "",
  redirectTo,
  notice = "",
}: {
  mode: FormMode;
  confirmationEmail?: string;
  redirectTo?: string;
  notice?: string;
}) {
  const router = useRouter();
  const [message, setMessage] = useState(notice);
  const [pending, setPending] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const passwordId = useId();
  const formRef = useRef<HTMLFormElement>(null);
  const details = content[mode];

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage("");
    setPending(true);
    const form = new FormData(event.currentTarget);
    const payload = Object.fromEntries(form.entries());

    try {
      const response = await fetch(details.endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = (await response.json()) as {
        message?: string;
        next?: string;
        email?: string;
      };
      if (!response.ok) {
        setMessage(result.message ?? "Please try again.");
        return;
      }
      const next = redirectTo ?? result.next ?? "/";
      if (mode === "signup" || mode === "forgot") {
        router.push(
          `${next}?email=${encodeURIComponent(result.email ?? String(payload.email))}`,
        );
        return;
      }
      router.push(next);
      router.refresh();
    } catch {
      setMessage("We could not reach the server. Please try again.");
    } finally {
      setPending(false);
    }
  }

  async function resend() {
    const email = formRef.current
      ? new FormData(formRef.current).get("email")?.toString()
      : undefined;
    if (!email) {
      setMessage("Enter your email address before requesting a new code.");
      return;
    }
    setPending(true);
    try {
      const response = await fetch("/api/auth/resend", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      const result = (await response.json()) as { message?: string };
      setMessage(
        result.message ??
          (response.ok
            ? "A new verification code has been sent."
            : "Please try again."),
      );
    } catch {
      setMessage("We could not reach the server. Please try again.");
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="auth-page">
      <section className="auth-card panel">
        <Link href="/" className="back">
          <ArrowLeft size={15} /> Back to Ticketto
        </Link>
        <span className="eyebrow">{details.label}</span>
        <h1>{details.title}</h1>
        <form ref={formRef} onSubmit={submit} className="auth-form">
          {mode === "signup" && (
            <label className="field">
              <span>Name</span>
              <input name="name" autoComplete="name" required />
            </label>
          )}
          <label className="field">
            <span>Email</span>
            <input
              name="email"
              type="email"
              autoComplete="email"
              defaultValue={confirmationEmail}
              required
            />
          </label>
          {(mode === "confirm" || mode === "reset") && (
            <label className="field">
              <span>Verification code</span>
              <input
                name="code"
                inputMode="numeric"
                autoComplete="one-time-code"
                required
              />
            </label>
          )}
          {mode !== "confirm" && mode !== "forgot" && (
            <div className="field">
              <label htmlFor={passwordId}>
                {mode === "reset" ? "New password" : "Password"}
              </label>
              <span className="password-field">
                <input
                  id={passwordId}
                  name="password"
                  type={showPassword ? "text" : "password"}
                  autoComplete={
                    mode === "login" ? "current-password" : "new-password"
                  }
                  aria-describedby={
                    mode === "login" ? undefined : `${passwordId}-hint`
                  }
                  minLength={8}
                  required
                />
                <button
                  type="button"
                  className="password-toggle"
                  aria-label={showPassword ? "Hide password" : "Show password"}
                  aria-pressed={showPassword}
                  onClick={() => setShowPassword(!showPassword)}
                >
                  {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </span>
              {mode !== "login" && (
                <small id={`${passwordId}-hint`} className="muted">
                  At least 8 characters.
                </small>
              )}
            </div>
          )}
          {message && (
            <p className="auth-message" role="status">
              {message}
            </p>
          )}
          <button className="button primary" type="submit" disabled={pending}>
            {pending ? "Please wait…" : details.submit}
          </button>
        </form>
        {mode === "confirm" ? (
          <button
            className="text-button"
            type="button"
            disabled={pending}
            onClick={resend}
          >
            Resend verification code
          </button>
        ) : mode === "forgot" || mode === "reset" ? (
          <p className="auth-switch small muted">
            {mode === "reset" && (
              <>
                No code? <Link href="/forgot-password">Send a new one</Link>
                {" · "}
              </>
            )}
            <Link href="/login">Back to sign in</Link>
          </p>
        ) : (
          <p className="auth-switch small muted">
            {mode === "login" && (
              <>
                <Link href="/forgot-password">Forgot your password?</Link>
                <br />
              </>
            )}
            {mode === "login" ? "New to Ticketto?" : "Already have an account?"}{" "}
            <Link href={mode === "login" ? "/signup" : "/login"}>
              {mode === "login" ? "Create an account" : "Sign in"}
            </Link>
          </p>
        )}
      </section>
    </main>
  );
}
