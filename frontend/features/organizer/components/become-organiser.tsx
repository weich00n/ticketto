"use client";

import { Button } from "@/components/ui/button";
import { Empty } from "@/components/ui/shared";
import { ApiError } from "@/lib/api/client";
import { becomeOrganiser } from "../api";
import { ArrowRight } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

export function BecomeOrganiser() {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState("");

  async function upgrade() {
    setPending(true);
    setMessage("");
    try {
      await becomeOrganiser();
      // The organiser group only appears in tokens issued after the upgrade.
      await fetch("/api/auth/refresh", { method: "POST" });
      router.refresh();
    } catch (error) {
      setMessage(
        error instanceof ApiError
          ? error.message
          : "We could not upgrade your account.",
      );
    } finally {
      setPending(false);
    }
  }

  return (
    <Empty
      title="Host your own events"
      text={
        message ||
        "Upgrade to an organiser account to create events and manage ticket sales."
      }
      action={
        <Button onClick={upgrade} disabled={pending}>
          {pending ? "Please wait…" : "Become an organiser"}{" "}
          <ArrowRight size={16} />
        </Button>
      }
    />
  );
}
