"use client";
import type { SessionUser } from "@/lib/auth/tokens";
import { ChevronDown, LayoutDashboard, LogOut, Ticket } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

const initialsOf = (name: string) =>
  name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0].toUpperCase())
    .join("") || "?";

export function ProfileMenu({ user }: { user: SessionUser }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  const signOut = async () => {
    setOpen(false);
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } finally {
      router.push("/");
      router.refresh();
    }
  };

  return (
    <div className="profile-menu" ref={ref}>
      <button
        className="profile"
        onClick={() => setOpen(!open)}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <span className="avatar">{initialsOf(user.name)}</span>
        <span className="profile-name">{user.name}</span>
        <ChevronDown size={15} className="profile-chevron" />
      </button>
      {open && (
        <div className="profile-dropdown" role="menu">
          <div className="profile-summary">
            <b>{user.name}</b>
            {user.email && user.email !== user.name && (
              <span>{user.email}</span>
            )}
          </div>
          <Link role="menuitem" href="/tickets" onClick={() => setOpen(false)}>
            <Ticket size={16} /> My Tickets
          </Link>
          <Link
            role="menuitem"
            href="/organizer"
            onClick={() => setOpen(false)}
          >
            <LayoutDashboard size={16} />
            {user.organiser ? "Organizer Dashboard" : "Become an organiser"}
          </Link>
          <button role="menuitem" onClick={signOut}>
            <LogOut size={16} /> Sign out
          </button>
        </div>
      )}
    </div>
  );
}
