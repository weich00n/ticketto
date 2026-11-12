const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function requiredString(value: unknown, label: string) {
  if (typeof value !== "string" || !value.trim())
    throw new Error(`${label} is required.`);
  return value.trim();
}

export function email(value: unknown) {
  const valueAsString = requiredString(value, "Email").toLowerCase();
  if (!emailPattern.test(valueAsString))
    throw new Error("Enter a valid email address.");
  return valueAsString;
}

export function password(value: unknown) {
  const valueAsString = requiredString(value, "Password");
  if (valueAsString.length < 8)
    throw new Error("Password must be at least 8 characters.");
  return valueAsString;
}

// Only same-site paths, so a crafted `?next=` link cannot send users elsewhere.
export function safeRedirectPath(value: unknown) {
  // Browsers drop tabs and newlines from a URL, so "/\t/host" would mean "//host".
  return typeof value === "string" &&
    !/[\u0000-\u001f\u007f]/.test(value) &&
    value.startsWith("/") &&
    !value.startsWith("//") &&
    !value.startsWith("/\\")
    ? value
    : undefined;
}
