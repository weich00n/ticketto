import { Platform } from "@/components/layout/platform";
import { demoDataEnabled, stripePublishableKey } from "@/lib/api/config";
import { currentUser } from "@/lib/auth/session";

// The app shell lives in a layout so it keeps its state, such as the toast and
// loaded events, while the visitor moves between screens. The pages only claim
// their URLs; Platform picks the screen from the pathname.
export default async function PlatformLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <>
      <Platform
        user={await currentUser()}
        demoData={demoDataEnabled()}
        stripeKey={stripePublishableKey()}
      />
      {children}
    </>
  );
}
