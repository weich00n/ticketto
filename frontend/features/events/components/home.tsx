"use client";

import { AnimatedUnderline } from "@/components/ui/animated-underline";
import { BlurFade } from "@/components/ui/blur-fade";
import { Button } from "@/components/ui/button";
import { Badge, EventCard } from "@/components/ui/shared";
import { categories } from "@/features/events/categories";
import { dateLabel, dayAndMonth } from "@/lib/format";
import { type Event } from "@/types/ticketing";
import {
  ArrowRight,
  ArrowUpRight,
  CalendarDays,
  Drama,
  Flower2,
  MapPin,
  Mic2,
  Music2,
  Search,
  ShieldCheck,
  Ticket,
  Trophy,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

const icons = [Ticket, Music2, Drama, Flower2, Trophy, Mic2];

export function Home({
  events,
  saved,
  save,
}: {
  events: Event[];
  saved: string[];
  save: (id: string) => void;
}) {
  const [category, setCategory] = useState("All Events");
  const [search, setSearch] = useState("");
  const router = useRouter();
  const [slide, setSlide] = useState(0);
  const featured = events.filter(
    (e) => !["Event cancelled", "Event ended", "Draft"].includes(e.status),
  );
  const hero = featured[slide] || events[0];
  const upcoming = events.filter((e) =>
    ["Upcoming sale", "Event updated"].includes(e.status),
  );
  // The spotlighted event stays in the listing too.
  const shown = featured
    .filter((e) => category === "All Events" || e.category === category)
    .slice(0, 3);
  return (
    <>
      <section className="hero container">
        <BlurFade>
          <div className="hero-copy">
            <span className="eyebrow">YOUR NEXT ADVENTURE STARTS HERE</span>
            <h1>
              A little wonder.
              <br />A <em>lasting memory.</em>
            </h1>
            <p>
              For the nights you’ll talk about for years. <br />
              Discover remarkable events, and find your way in.
            </p>
            <Button asChild>
              <Link href="/events">
                Browse Events <ArrowRight size={16} />
              </Link>
            </Button>
            <div className="hero-footnote">Real moments. Remarkably easy.</div>
          </div>
        </BlurFade>
        <div className="hero-art">
          {hero && (
            <div className="hero-ticket">
              <div
                className="hero-photo"
                style={{
                  backgroundImage: `linear-gradient(180deg,rgba(20,19,14,.05),rgba(20,19,14,.75)),url(${hero.image})`,
                }}
              >
                <span className="hero-tag">
                  <span /> THE SPOTLIGHT
                </span>
                <div>
                  <span className="eyebrow">
                    {hero.category ? `${hero.category} · ` : ""}LIVE IN
                    SINGAPORE
                  </span>
                  <h2>{hero.name}</h2>
                  <p>{hero.subtitle}</p>
                </div>
              </div>
              <div className="hero-ticket-bottom">
                <div>
                  <span>
                    <CalendarDays size={13} />
                    {dateLabel(hero.date)}
                  </span>
                  <span>
                    <MapPin size={13} />
                    {hero.venue}
                  </span>
                </div>
                <Link
                  href={`/events/${hero.id}`}
                  aria-label={`Explore ${hero.name}`}
                >
                  <ArrowUpRight size={22} />
                </Link>
              </div>
              <div className="ticket-serial">
                ADMIT ONE TO SOMETHING EXTRAORDINARY <span>№ 001426</span>
              </div>
            </div>
          )}
          <div className="carousel-dots">
            {featured.slice(0, 3).map((e, i) => (
              <button
                key={e.id}
                className={slide === i ? "selected" : ""}
                aria-label={`Feature ${e.name}`}
                aria-pressed={slide === i}
                onClick={() => setSlide(i)}
              />
            ))}
          </div>
        </div>
      </section>
      <section className="search-band container">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const data = new FormData(e.currentTarget);
            const month = String(data.get("date") || "").split(" ")[0];
            router.push(
              `/events?q=${encodeURIComponent(search)}&month=${({ October: "10", November: "11", December: "12" } as Record<string, string>)[month] || ""}`,
            );
          }}
        >
          <div className="search-main">
            <Search size={19} />
            <label className="sr-only" htmlFor="home-search">
              Search events
            </label>
            <input
              id="home-search"
              placeholder="What would you like to experience?"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <label className="search-select">
            <CalendarDays size={17} />
            <select aria-label="Event date" name="date">
              <option>Any date</option>
              <option>October 2026</option>
              <option>November 2026</option>
              <option>December 2026</option>
            </select>
          </label>
          <Button type="submit">
            Find Events <ArrowRight size={15} />
          </Button>
        </form>
      </section>
      <section className="container discovery">
        <div className="section-heading">
          <div>
            <span className="eyebrow">FIND YOUR KIND OF EXTRAORDINARY</span>
            <h2>There’s a place for you.</h2>
          </div>
          <Link href="/events" className="text-link">
            <AnimatedUnderline>Explore all events</AnimatedUnderline>{" "}
            <ArrowRight size={17} />
          </Link>
        </div>
        <div className="category-tabs">
          {categories.map((c, i) => {
            const Icon = icons[i];
            return (
              <button
                key={c}
                className={category === c ? "selected" : ""}
                onClick={() => setCategory(c)}
                aria-pressed={category === c}
              >
                <Icon size={17} />
                {c}
              </button>
            );
          })}
        </div>
        <div className="cards-grid">
          {shown.map((e) => (
            <EventCard
              key={e.id}
              event={e}
              saved={saved.includes(e.id)}
              save={save}
            />
          ))}
        </div>
      </section>
      <section className="trust-strip container">
        <div className="trust-intro">
          <div>
            <h3>
              Great moments.
              <br />
              Peace of mind.
            </h3>
          </div>
        </div>
        <div>
          <ShieldCheck />
          <section>
            <h4>Your place, protected</h4>
            <p>
              Fair queues. Secure reservations.
              <br />
              No frantic refreshing.
            </p>
          </section>
        </div>
        <div>
          <Ticket />
          <section>
            <h4>From discovery to the door</h4>
            <p>
              A simple booking experience.
              <br />
              Your tickets, always close.
            </p>
          </section>
        </div>
        <div>
          <Ticket />
          <section>
            <h4>A little magic. A lot of memories.</h4>
            <p>
              Thoughtfully chosen experiences.
              <br />
              Made for being there.
            </p>
          </section>
        </div>
      </section>
      {upcoming.length > 0 && (
        <section className="container upcoming">
          <div className="section-heading">
            <div>
              <span className="eyebrow">GOOD THINGS ARE ON THE HORIZON</span>
              <h2>Worth marking your calendar.</h2>
            </div>
            <Link className="text-link" href="/events">
              See what’s coming <ArrowRight size={17} />
            </Link>
          </div>
          <div className="upcoming-list">
            {upcoming.map((e) => (
              <Link href={`/events/${e.id}`} key={e.id}>
                <div className="date-block">
                  <b>{dayAndMonth(e.date).day}</b>
                  <span>{dayAndMonth(e.date).month}</span>
                </div>
                <div>
                  <span className="eyebrow">{e.category}</span>
                  <h3>{e.name}</h3>
                  <p>{e.venue}</p>
                </div>
                <Badge status={e.status} />
                <ArrowUpRight />
              </Link>
            ))}
          </div>
        </section>
      )}
    </>
  );
}
