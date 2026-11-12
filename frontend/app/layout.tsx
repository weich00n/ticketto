import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = {
  title: {
    default: "Ticketto — A little wonder. A lasting memory.",
    template: "%s — Ticketto",
  },
  description:
    "Discover extraordinary live experiences. Your next unforgettable moment starts here.",
  icons: { icon: "/favicon.svg" },
};
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
