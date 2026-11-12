"use client";
export default function Error({ reset }: { reset: () => void }) {
  return (
    <main className="container section">
      <h1>Something interrupted the experience.</h1>
      <p>Please try opening the page again.</p>
      <button className="button primary" onClick={reset}>
        Try again
      </button>
    </main>
  );
}
