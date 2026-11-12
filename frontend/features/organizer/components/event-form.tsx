"use client";

import { Button } from "@/components/ui/button";
import { Field, PageHeading } from "@/components/ui/shared";
import { lowestPrice } from "@/features/events/api";
import { categories } from "@/features/events/categories";
import {
  imageProblem,
  imageTypes,
  maxImageBytes,
} from "@/features/events/images";
import { PublishError } from "../api";
import { ApiError } from "@/lib/api/client";
import { type Event, type EventStatus } from "@/types/ticketing";
import { ArrowLeft, ArrowRight } from "lucide-react";
import Link from "next/link";
import { useState, type ChangeEvent, type FormEvent } from "react";
import {
  earliestRelease,
  releaseLeadMinutes,
  scheduleProblem,
  serviceScheduleProblem,
  type ScheduleProblem,
} from "../schedule";
import { TimeSelect } from "./time-select";

export function EventForm({
  event,
  save,
  saveNote = "Changes are saved locally in this browser.",
  uploadImage = false,
}: {
  saveNote?: string;
  event?: Event;
  // Pick the banner from a file, for the Event Service, instead of typing a URL.
  uploadImage?: boolean;
  // A rejected promise shows its message above the buttons.
  save: (event: Event, image?: File) => void | Promise<void>;
}) {
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [waiting, setWaiting] = useState(event?.waitingRoom ?? true);
  const [imageUrl, setImageUrl] = useState(event?.image || "/opera.jpg");
  const [imageFile, setImageFile] = useState<File>();
  const [imagePreview, setImagePreview] = useState<string>();
  const [imageError, setImageError] = useState("");
  const [startTime, setStartTime] = useState(event?.time || "19:30");
  const [releaseDate, setReleaseDate] = useState(
    event?.release.slice(0, 10) ?? "",
  );
  const [releaseTime, setReleaseTime] = useState(
    event?.release.slice(11, 16) || "10:00",
  );
  const [timeProblem, setTimeProblem] = useState<ScheduleProblem>();
  const showTimeProblem = (problem: ScheduleProblem) => {
    setTimeProblem(problem);
    setError("");
    // Take the organiser to the field that needs changing.
    document
      .getElementById(
        problem.field === "release" ? "release-date" : "event-date",
      )
      ?.focus();
  };
  const useEarliestRelease = () => {
    const earliest = earliestRelease();
    setReleaseDate(earliest.date);
    setReleaseTime(earliest.time);
    setTimeProblem(undefined);
  };
  const pickImage = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    setImageFile(undefined);
    setImagePreview(undefined);
    setImageError("");
    if (!file) return;
    const problem = imageProblem(file);
    if (problem) {
      setImageError(problem);
      e.target.value = "";
      return;
    }
    setImageFile(file);
    const reader = new FileReader();
    reader.onload = () => setImagePreview(String(reader.result));
    reader.readAsDataURL(file);
  };
  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = e.currentTarget;
    const data = new FormData(form);
    const action = (e.nativeEvent as SubmitEvent).submitter?.getAttribute(
      "value",
    );
    const status: EventStatus =
      action === "draft" ? "Draft" : waiting ? "High demand" : "On sale";
    if (imageError) {
      setError(imageError);
      return;
    }
    const date = String(data.get("date"));
    const release = String(data.get("release"));
    const problem = scheduleProblem(
      { date, time: String(data.get("time")), release },
      status !== "Draft",
    );
    if (problem) {
      showTimeProblem(problem);
      return;
    }
    const cats = [1, 2, 3].map((n) => ({
      id: `cat${n}`,
      name: `CAT ${n}`,
      price: Number(data.get(`price${n}`)),
      available: Number(data.get(`qty${n}`)),
      benefits:
        n === 1
          ? "Premium view · closest to the stage"
          : n === 2
            ? "Excellent view · the perfect balance"
            : "Standard admission · be part of it all",
    }));
    if (cats.every((c) => c.available === 0) && status !== "Draft") {
      setError(
        "Add inventory to at least one ticket category before publishing.",
      );
      return;
    }
    setPending(true);
    setError("");
    setTimeProblem(undefined);
    try {
      await save(
        {
          id: event?.id || `event-${Date.now().toString(36)}`,
          name: String(data.get("name")).trim(),
          subtitle: String(data.get("subtitle")),
          description: String(data.get("description")).trim(),
          category: String(data.get("category")),
          date,
          time: String(data.get("time")),
          venue: String(data.get("venue")).trim(),
          location: "Singapore",
          image: imageUrl,
          status,
          price: lowestPrice(cats),
          categories: cats,
          release,
          maxTickets: 5,
          waitingRoom: waiting,
          organizer: event?.organizer || "Your Event Studio",
        },
        imageFile,
      );
    } catch (err) {
      const problem =
        err instanceof ApiError || err instanceof PublishError
          ? serviceScheduleProblem(err.details)
          : undefined;
      if (problem) showTimeProblem(problem);
      else if (err instanceof PublishError)
        // Saving again changes this same draft, so it is safe to try again.
        setError(
          `Your draft is saved, but it could not be published: ${err.message}`,
        );
      else
        setError(
          err instanceof Error ? err.message : "The event could not be saved.",
        );
    } finally {
      setPending(false);
    }
  };
  return (
    <div className="container section form-container">
      <Link href="/organizer" className="back">
        <ArrowLeft size={15} /> Organizer Dashboard
      </Link>
      <PageHeading
        label="MAKE SOMETHING REMARKABLE"
        title={
          event
            ? "Give your event its next chapter."
            : "Every great event starts with an idea."
        }
        text="Set the scene. Invite your audience. We’ll take care of the tickets."
      />
      <form onSubmit={submit}>
        <section className="panel form-section">
          <div className="form-section-title">
            <span>01</span>
            <div>
              <h2>The essentials</h2>
              <p>Introduce your experience to the world.</p>
            </div>
          </div>
          <Field label="Event name">
            <input
              name="name"
              required
              minLength={3}
              maxLength={90}
              defaultValue={event?.name}
              placeholder="Give your event a memorable name"
            />
          </Field>
          <div className="form-grid">
            <Field label="Category">
              <select
                name="category"
                defaultValue={event?.category || "Concerts"}
              >
                {categories.slice(1).map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </select>
            </Field>
            <Field label="Short tagline">
              <input
                name="subtitle"
                maxLength={100}
                defaultValue={event?.subtitle}
                placeholder="One line to set the mood"
              />
            </Field>
          </div>
          <Field label="Event description">
            <textarea
              name="description"
              required
              minLength={20}
              rows={5}
              defaultValue={event?.description}
              placeholder="Tell guests what makes this experience special…"
            />
          </Field>
          {uploadImage ? (
            <>
              <Field label="Banner image">
                <input
                  type="file"
                  name="image"
                  accept={imageTypes.join(",")}
                  onChange={pickImage}
                />
              </Field>
              <p className="small muted">
                JPEG, PNG or WebP, up to {maxImageBytes / 1024} KB. Without one,
                a default image is used.
              </p>
              {imageError && (
                <div className="error-message" role="alert">
                  {imageError}
                </div>
              )}
              {(imagePreview || event?.image) && (
                // eslint-disable-next-line @next/next/no-img-element -- a local preview, not an optimisable asset.
                <img
                  className="summary-image"
                  src={imagePreview ?? event?.image}
                  alt="Banner preview"
                />
              )}
            </>
          ) : (
            <>
              <Field label="Banner image URL">
                <input
                  type="text"
                  required
                  pattern="(https?://.*|/[^/].*)"
                  title="Enter an https:// image URL or a local path such as /opera.jpg"
                  value={imageUrl}
                  onChange={(e) => setImageUrl(e.target.value)}
                />
              </Field>
              <p className="small muted">
                Use a public image URL or a local asset path. A theatre image is
                provided.
              </p>
            </>
          )}
        </section>
        <section className="panel form-section">
          <div className="form-section-title">
            <span>02</span>
            <div>
              <h2>A time and a place</h2>
              <p>Bring your audience to the right moment.</p>
            </div>
          </div>
          <div className="form-grid">
            <Field label="Event date">
              <input
                id="event-date"
                name="date"
                type="date"
                required
                defaultValue={event?.date}
                aria-invalid={timeProblem?.field === "start" || undefined}
                onChange={() => setTimeProblem(undefined)}
              />
            </Field>
            <Field label="Start time (Singapore)">
              <TimeSelect
                name="time"
                value={startTime}
                onChange={(time) => {
                  setStartTime(time);
                  setTimeProblem(undefined);
                }}
                invalid={timeProblem?.field === "start"}
              />
            </Field>
          </div>
          {timeProblem?.field === "start" && (
            <p className="field-error" role="alert">
              {timeProblem.message}
            </p>
          )}
          <Field label="Venue in Singapore">
            <input
              name="venue"
              required
              minLength={3}
              defaultValue={event?.venue}
              placeholder="Venue name and address"
            />
          </Field>
        </section>
        <section className="panel form-section">
          <div className="form-section-title">
            <span>03</span>
            <div>
              <h2>Make room for your audience</h2>
              <p>Set your categories, prices and inventory.</p>
            </div>
          </div>
          {[1, 2, 3].map((n, i) => (
            <div className="ticket-form-row" key={n}>
              <b>CAT {n}</b>
              <Field label="Price (SGD)">
                <input
                  type="number"
                  required
                  name={`price${n}`}
                  min="0"
                  max="10000"
                  step="1"
                  defaultValue={
                    event?.categories[i]?.price ??
                    (n === 1 ? 128 : n === 2 ? 98 : 68)
                  }
                />
              </Field>
              <Field label="Ticket quantity">
                <input
                  type="number"
                  required
                  name={`qty${n}`}
                  min="0"
                  max="50000"
                  step="1"
                  defaultValue={event?.categories[i]?.available ?? 100}
                />
              </Field>
            </div>
          ))}
          <div className="form-grid">
            <div className="field">
              <span>Booking limit</span>
              <p className="small muted">
                Buyers can book up to 5 tickets per order.
              </p>
            </div>
            <div className="field" role="group" aria-labelledby="release-label">
              <span id="release-label">Ticket release (Singapore time)</span>
              <div className="field-pair">
                <input
                  id="release-date"
                  type="date"
                  required
                  aria-label="Ticket release date"
                  value={releaseDate}
                  aria-invalid={timeProblem?.field === "release" || undefined}
                  onChange={(e) => {
                    setReleaseDate(e.target.value);
                    setTimeProblem(undefined);
                  }}
                />
                <TimeSelect
                  value={releaseTime}
                  label="Ticket release time"
                  onChange={(time) => {
                    setReleaseTime(time);
                    setTimeProblem(undefined);
                  }}
                  invalid={timeProblem?.field === "release"}
                />
              </div>
              <input
                type="hidden"
                name="release"
                value={releaseDate ? `${releaseDate}T${releaseTime}` : ""}
              />
              {timeProblem?.field === "release" ? (
                <p className="field-error" role="alert">
                  {timeProblem.message}
                </p>
              ) : (
                <p className="field-hint">
                  Tickets go on sale at this time. The waiting room opens{" "}
                  {releaseLeadMinutes} minutes earlier, so choose a time at
                  least {releaseLeadMinutes} minutes from now.
                </p>
              )}
              <button
                type="button"
                className="text-button"
                onClick={useEarliestRelease}
              >
                Use the earliest time
              </button>
            </div>
          </div>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={waiting}
              onChange={(e) => setWaiting(e.target.checked)}
            />
            <span>
              <b>Enable a waiting room</b>
              <small>
                Give everyone a calmer, fairer way into high-demand releases.
              </small>
            </span>
          </label>
        </section>
        {error && (
          <div className="error-message" role="alert">
            {error}
          </div>
        )}
        <div className="form-actions">
          <p className="muted small">{saveNote}</p>
          <Button
            type="submit"
            variant="outline"
            name="action"
            value="draft"
            disabled={pending}
          >
            Save Draft
          </Button>
          <Button
            type="submit"
            name="action"
            value="publish"
            disabled={pending}
          >
            {event && event.status !== "Draft"
              ? "Publish Updates"
              : "Publish Event"}{" "}
            <ArrowRight size={16} />
          </Button>
        </div>
      </form>
    </div>
  );
}
