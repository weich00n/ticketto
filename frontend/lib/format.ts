export const money = (n: number) =>
  new Intl.NumberFormat("en-SG", {
    style: "currency",
    currency: "SGD",
    // Whole dollars stay short (S$13); anything else shows its cents (S$12.50).
    minimumFractionDigits: Number.isInteger(n) ? 0 : 2,
    maximumFractionDigits: 2,
  }).format(n);
export const dateLabel = (date: string) =>
  !date
    ? "Date to be confirmed"
    : new Date(date + "T12:00:00").toLocaleDateString("en-GB", {
        day: "numeric",
        month: "short",
        year: "numeric",
      });
// A moment shown in Singapore time, where every event is, whatever the viewer's zone.
export const singaporeDateTime = (iso: string) =>
  `${new Date(iso).toLocaleString("en-SG", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Singapore",
  })} SGT`;
// The day of the month and short month name of a "YYYY-MM-DD" date. It is read
// at noon, so the viewer's time zone cannot move it to the day before or after.
export const dayAndMonth = (date: string) => {
  const day = new Date(`${date}T12:00:00`);
  return {
    day: day.getDate(),
    month: day.toLocaleDateString("en", { month: "short" }),
  };
};
export const timeAgo = (date: string, now = Date.now()) => {
  const minutes = Math.floor((now - Date.parse(date)) / 60_000);
  if (minutes < 1) return "Just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours}h ago` : `${Math.floor(hours / 24)}d ago`;
};
