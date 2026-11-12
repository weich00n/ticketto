/* eslint-disable react-hooks/set-state-in-effect -- Synchronize browser-only storage and navigation state after hydration. */
"use client";

import { Button } from "@/components/ui/button";
import { Empty, EventCard, PageHeading } from "@/components/ui/shared";
import { categories } from "@/features/events/categories";
import { type Event } from "@/types/ticketing";
import {
  Bookmark,
  Drama,
  Flower2,
  Mic2,
  Music2,
  Search,
  Ticket,
  Trophy,
} from "lucide-react";
import { useEffect, useState } from "react";

const icons = [Ticket, Music2, Drama, Flower2, Trophy, Mic2];

export function Browse({
  events,
  saved,
  save,
}: {
  events: Event[];
  saved: string[];
  save: (id: string) => void;
}) {
  const [q, setQ] = useState("");
  const [category, setCategory] = useState("All Events");
  const [month, setMonth] = useState("");
  const [onlySaved, setOnlySaved] = useState(false);
  useEffect(() => {
    setQ(new URLSearchParams(window.location.search).get("q") || "");
    setMonth(new URLSearchParams(window.location.search).get("month") || "");
  }, []);
  const results = events.filter(
    (e) =>
      e.status !== "Draft" &&
      e.name.toLowerCase().includes(q.toLowerCase()) &&
      (category === "All Events" || e.category === category) &&
      (!month || e.date.slice(5, 7) === month) &&
      (!onlySaved || saved.includes(e.id)),
  );
  return (
    <div className="container section">
      <PageHeading
        label="YOUR NEXT STORY"
        title="Find your extraordinary."
        text="The stage is set. All that’s missing is you."
      />
      <div className="filter-panel">
        <div className="input-icon">
          <Search size={18} />
          <input
            aria-label="Search events"
            placeholder="Search events, artists, experiences…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
        <select
          aria-label="Filter by month"
          value={month}
          onChange={(e) => setMonth(e.target.value)}
        >
          <option value="">Any date</option>
          <option value="09">September 2026</option>
          <option value="10">October 2026</option>
          <option value="11">November 2026</option>
          <option value="12">December 2026</option>
        </select>
        <button
          className={`button ${onlySaved ? "primary" : "outline"}`}
          onClick={() => setOnlySaved(!onlySaved)}
        >
          <Bookmark size={16} />
          Saved
        </button>
      </div>
      <div className="category-tabs">
        {categories.map((c, i) => {
          const Icon = icons[i];
          return (
            <button
              key={c}
              onClick={() => setCategory(c)}
              aria-pressed={category === c}
              className={category === c ? "selected" : ""}
            >
              <Icon size={16} />
              {c}
            </button>
          );
        })}
      </div>
      <p className="results-count">
        {results.length} experiences waiting to be discovered
      </p>
      <div className="cards-grid">
        {results.map((e) => (
          <EventCard
            key={e.id}
            event={e}
            saved={saved.includes(e.id)}
            save={save}
          />
        ))}
      </div>
      {!results.length && (
        <Empty
          title="No events found"
          text="Try another search or adjust your filters."
          action={
            <Button
              onClick={() => {
                setQ("");
                setMonth("");
                setCategory("All Events");
                setOnlySaved(false);
              }}
            >
              Clear filters
            </Button>
          }
        />
      )}
    </div>
  );
}
