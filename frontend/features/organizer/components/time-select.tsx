"use client";

const pad = (n: number) => String(n).padStart(2, "0");

// Every quarter-hour of the day, as "HH:MM".
const slots = Array.from(
  { length: 96 },
  (_, i) => `${pad(Math.floor(i / 4))}:${pad((i % 4) * 15)}`,
);

export const timeLabel = (time: string) => {
  const [hour, minute] = time.split(":").map(Number);
  return `${hour % 12 || 12}:${pad(minute)} ${hour < 12 ? "am" : "pm"}`;
};

// A list of times to pick from, in place of the browser's time field, whose
// hour, minute and am/pm parts each scroll on their own.
export function TimeSelect({
  name,
  value,
  onChange,
  label,
  invalid,
}: {
  name?: string;
  value: string;
  onChange: (time: string) => void;
  label?: string;
  invalid?: boolean;
}) {
  // An event saved with a time between the quarter-hours still shows it.
  const options = slots.includes(value) ? slots : [...slots, value].sort();
  return (
    <select
      name={name}
      value={value}
      aria-label={label}
      aria-invalid={invalid || undefined}
      onChange={(e) => onChange(e.target.value)}
    >
      {options.map((time) => (
        <option key={time} value={time}>
          {timeLabel(time)}
        </option>
      ))}
    </select>
  );
}
