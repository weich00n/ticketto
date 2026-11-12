/* eslint-disable react-hooks/set-state-in-effect -- Reset the request state when the request changes. */
"use client";
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "./client";

export type Remote<T> =
  | { status: "loading" }
  | { status: "error"; message: string; error: unknown }
  | { status: "ready"; data: T };

export const errorMessage = (error: unknown, fallback: string) =>
  error instanceof ApiError ? error.message : fallback;

// Loads data for a screen. `key` identifies the request: when it changes the
// data is loaded again, and `reload` loads it again on demand.
export function useRemote<T>(
  key: string,
  load: () => Promise<T>,
  fallbackMessage = "This page could not be loaded. Please try again.",
) {
  const [state, setState] = useState<Remote<T>>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const reload = useCallback(() => setAttempt((n) => n + 1), []);

  useEffect(() => {
    let active = true;
    setState({ status: "loading" });
    load()
      .then((data) => active && setState({ status: "ready", data }))
      .catch(
        (error) =>
          active &&
          setState({
            status: "error",
            message: errorMessage(error, fallbackMessage),
            error,
          }),
      );
    return () => {
      active = false;
    };
    // `load` is recreated on every render; `key` says when it asks for something new.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, attempt]);

  return { state, reload };
}
