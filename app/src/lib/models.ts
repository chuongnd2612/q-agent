/**
 * Claude model catalog — served by the API, not hardcoded here (#715).
 *
 * The SPA used to keep its own copy, which is a third place to be wrong and was: it
 * offered a date-suffixed Haiku id and no Opus 5, while the backend priced Sonnet 5 at
 * $3/$15 instead of $2/$10. A dropdown that disagrees with what the server bills for is
 * worse than no dropdown.
 *
 * `FALLBACK_MODEL_OPTIONS` covers the moment before the fetch lands, and an unreachable
 * API — a Settings page with an empty model dropdown looks broken, and a user cannot
 * tell that from "this deployment offers no models".
 */
export const FALLBACK_MODEL_OPTIONS = [
  { value: "claude-opus-5", label: "Opus 5 — highest quality" },
  { value: "claude-sonnet-5", label: "Sonnet 5 — balanced" },
  { value: "claude-haiku-4-5", label: "Haiku 4.5 — fastest" },
];
