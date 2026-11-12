import { localDate, localTime, toUtcIso } from "@/features/events/api";

// Buyers can join the waiting room this long before tickets are released, so the
// Event Service only publishes an event whose release is at least this far away.
export const releaseLeadMinutes = 15;

const minute = 60_000;
const slot = 15 * minute;

export type ScheduleProblem = { field: "start" | "release"; message: string };

const at = (local: string) => Date.parse(toUtcIso(local));

const leadMessage = `Ticket release must be at least ${releaseLeadMinutes} minutes from now, because buyers can join the waiting room ${releaseLeadMinutes} minutes before it.`;

// What is wrong with the chosen times, if anything. Form values are Singapore
// time. A draft only needs a release in the future; publishing needs the lead.
export function scheduleProblem(
  { date, time, release }: { date: string; time: string; release: string },
  publish: boolean,
  now = Date.now(),
): ScheduleProblem | undefined {
  if (!release)
    return {
      field: "release",
      message: "Choose a date and time for the ticket release.",
    };
  const start = at(`${date}T${time}`);
  const releaseAt = at(release);
  if (start <= now)
    return {
      field: "start",
      message: "Choose an event date and time in the future.",
    };
  if (publish && releaseAt < now + releaseLeadMinutes * minute)
    return { field: "release", message: leadMessage };
  if (releaseAt <= now)
    return {
      field: "release",
      message: "Ticket release must be in the future.",
    };
  if (releaseAt >= start)
    return {
      field: "release",
      message: "Ticket release must be before the event begins.",
    };
}

// The same problems as the Event Service reports them, in the form's words.
export function serviceScheduleProblem(
  details?: Record<string, string>,
): ScheduleProblem | undefined {
  const release = details?.ticket_release_at;
  if (release)
    return {
      field: "release",
      message: /minutes/.test(release)
        ? leadMessage
        : `Ticket release: ${release}`,
    };
  const start = details?.starts_at;
  if (start) return { field: "start", message: `Event start: ${start}` };
}

// The first quarter-hour that is at least the lead away, with a minute to spare
// for the time it takes to submit.
export function earliestRelease(now = Date.now()) {
  const ms =
    Math.ceil((now + releaseLeadMinutes * minute + minute) / slot) * slot;
  const iso = new Date(ms).toISOString();
  return { date: localDate(iso), time: localTime(iso) };
}
