// The proxy joins these segments onto the gateway URL, so a segment that
// climbs out of the path ("..") or smuggles in a separator could reach a
// route the browser was never meant to call.
export const isSafeProxyPath = (segments: string[]) =>
  segments.length > 0 &&
  segments.every(
    (segment) =>
      segment !== "" &&
      segment !== "." &&
      segment !== ".." &&
      !/[/\\\u0000-\u001f\u007f]/.test(segment),
  );
