// The browser-tab title for a screen. An event's own page is named after the
// event once it is loaded, so it has no fixed title here.
export function pageTitle(path: string): string | undefined {
  const parts = path.split("/").filter(Boolean);
  const [first, second, third] = parts;
  if (!first) return undefined;
  if (first === "events" && !second) return "Browse events";
  if (first === "events" && third === "tickets") return "Choose your tickets";
  if (first === "events") return undefined;
  if (first === "queue") return "Waiting room";
  if (first === "checkout" && second === "success") return "Booking confirmed";
  if (first === "checkout") return "Checkout";
  if (first === "tickets" && !second) return "My tickets";
  if (first === "tickets") return "Your ticket";
  if (first === "organizer" && !second) return "Organiser dashboard";
  if (first === "organizer" && second === "events" && third === "new")
    return "Create an event";
  if (first === "organizer" && parts[3] === "edit") return "Edit event";
  if (first === "organizer") return "Manage event";
  return undefined;
}
