import { ApiError } from "@/lib/api/client";

// A poll that keeps failing is reported to the buyer after this many tries in a row.
export const maxPollFailures = 5;

// A dropped connection, a busy server or a gateway hiccup passes by itself, so
// the next poll is worth trying. A refusal from the service (for example,
// "queue not open" or "not signed in") will not change by asking again.
export function isTransient(error: unknown) {
  if (!(error instanceof ApiError)) return true;
  return error.status >= 500 || error.status === 408 || error.status === 429;
}
