/**
 * CodeRev Bot — two-pane review workspace.
 *
 * Fetches GET /api/reviews once on mount. The dropdown selection filters the
 * already-fetched array. Manual mode POSTs a snippet to /api/reviews/manual,
 * prepends the returned row to that same array, and renders it through the
 * same DiffView/ReviewView path as webhook PRs.
 *
 * Accent choice: indigo-600 is used for primary actions only (the retry
 * button and focus rings). Everything else is slate.
 */

import { useEffect, useMemo, useState } from "react";

const REPO_URL = "https://github.com/mayankranjan-dev/coderev";

const SEVERITY_STYLES = {
  CRITICAL: "bg-red-50 text-red-700",
  WARNING: "bg-amber-50 text-amber-700",
  NITPICK: "bg-slate-100 text-slate-600",
};

const STATUS_LABELS = {
  pending: "Pending — diff recorded, review not yet generated.",
  llm_offline_pending:
    "Review pending — the local model was unreachable when this code was submitted.",
  reviewed: null,
};

// Must match repo_name set by POST /api/reviews/manual in main.py.
const MANUAL_REPO_NAME = "Manual Submission";

function isManualReview(review) {
  return review.repo_name === MANUAL_REPO_NAME;
}

/**
 * SQLite drops tzinfo, so created_at comes back without an offset even
 * though the backend writes UTC. Treat offset-less timestamps as UTC.
 */
function formatTimestamp(value) {
  if (!value) return "";
  const hasZone = /(?:[zZ]|[+-]\d{2}:\d{2})$/.test(value);
  const date = new Date(hasZone ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** Dropdown label. Manual rows have no PR number/title, so use the time. */
function reviewLabel(review) {
  if (isManualReview(review)) {
    return `${review.repo_name} — ${formatTimestamp(review.created_at)}`;
  }
  return `${review.repo_name} #${review.pr_number} — ${review.pr_title}`;
}

/**
 * Split review_text into severity-tagged findings.
 *
 * Mirrors the backend's line-prefix parser: a line counts as a finding when
 * it starts with CRITICAL / WARNING / NITPICK followed by a colon or space.
 * Lines that match nothing are kept as untagged prose so no model output is
 * silently dropped.
 */
function parseFindings(reviewText) {
  if (!reviewText) return [];

  return reviewText
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line, index) => {
      const match = line.match(/^(CRITICAL|WARNING|NITPICK)[:\s]\s*(.*)$/i);

      if (match) {
        return {
          key: index,
          severity: match[1].toUpperCase(),
          text: match[2] || line,
        };
      }

      return { key: index, severity: null, text: line };
    });
}

function Spinner() {
  return (
    <span
      className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-slate-600"
      aria-hidden="true"
    />
  );
}

/** Per-line classes for a unified diff. Muted, flat, readable. */
function diffLineClass(line) {
  if (line.startsWith("+++") || line.startsWith("---")) return "text-slate-500";
  if (line.startsWith("@@")) return "text-slate-500";
  if (line.startsWith("diff ") || line.startsWith("index ")) return "text-slate-400";
  if (line.startsWith("+")) return "bg-emerald-50 text-emerald-800";
  if (line.startsWith("-")) return "bg-red-50 text-red-800";
  return "text-slate-700";
}

function DiffView({ diffText }) {
  const lines = useMemo(() => (diffText ? diffText.split("\n") : []), [diffText]);

  if (!diffText) {
    return (
      <p className="p-4 text-sm text-slate-500">
        No diff was stored for this pull request.
      </p>
    );
  }

  return (
    <pre className="min-w-full p-0 font-mono text-xs leading-5">
      <code>
        {lines.map((line, index) => (
          <div key={index} className={`whitespace-pre px-4 ${diffLineClass(line)}`}>
            {line || " "}
          </div>
        ))}
      </code>
    </pre>
  );
}

function ReviewView({ review }) {
  const findings = useMemo(() => parseFindings(review.review_text), [review.review_text]);
  const statusNote = STATUS_LABELS[review.status];

  if (statusNote) {
    return <p className="p-4 text-sm text-slate-500">{statusNote}</p>;
  }

  if (findings.length === 0) {
    return (
      <p className="p-4 text-sm text-slate-500">
        The review completed without any findings.
      </p>
    );
  }

  return (
    <ul className="divide-y divide-slate-200">
      {findings.map((finding) => (
        <li key={finding.key} className="flex gap-3 px-4 py-3">
          {finding.severity ? (
            <span
              className={`mt-0.5 w-16 shrink-0 self-start rounded py-0.5 text-center text-[11px] font-medium ${
                SEVERITY_STYLES[finding.severity]
              }`}
            >
              {finding.severity}
            </span>
          ) : (
            <span className="mt-0.5 w-16 shrink-0 self-start py-0.5 text-center text-[11px] text-slate-400">
              —
            </span>
          )}
          <p className="text-sm leading-6 text-slate-900">{finding.text}</p>
        </li>
      ))}
    </ul>
  );
}

function Pane({ title, action, children }) {
  return (
    <section className="flex min-h-0 min-w-0 flex-col border border-slate-200 bg-white">
      <header className="flex shrink-0 items-center justify-between gap-4 border-b border-slate-200 px-4 py-2.5">
        <h2 className="text-sm font-medium text-slate-900">{title}</h2>
        {action}
      </header>
      <div className="min-h-0 flex-1 overflow-auto">{children}</div>
    </section>
  );
}

function ManualEditor({ code, onChange, onSubmit, submitting, error }) {
  const canSubmit = !submitting && code.trim().length > 0;

  return (
    <form onSubmit={onSubmit} className="flex h-full flex-col gap-3 p-4">
      <label htmlFor="manual-code" className="sr-only">
        Code to review
      </label>
      <textarea
        id="manual-code"
        value={code}
        onChange={(event) => onChange(event.target.value)}
        spellCheck={false}
        placeholder="Paste code to review"
        className="min-h-0 flex-1 resize-none rounded border border-slate-200 bg-white p-3 font-mono text-xs leading-5 text-slate-900 placeholder:text-slate-500 focus:border-slate-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600"
      />
      {error ? (
        <p role="alert" className="text-sm text-red-700">
          {error}
        </p>
      ) : null}
      <div className="flex justify-end">
        <button
          type="submit"
          disabled={!canSubmit}
          className="rounded bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-indigo-600"
        >
          Submit for Review
        </button>
      </div>
    </form>
  );
}

export default function App() {
  const [reviews, setReviews] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [mode, setMode] = useState("browse"); // "browse" | "manual"
  const [manualCode, setManualCode] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);

  useEffect(() => {
    const controller = new AbortController();

    async function load() {
      setLoading(true);
      setError(null);

      try {
        const response = await fetch("/api/reviews", { signal: controller.signal });

        if (!response.ok) {
          throw new Error(`Request failed with status ${response.status}`);
        }

        const data = await response.json();
        const rows = Array.isArray(data) ? data : [];

        setReviews(rows);
        setSelectedId(rows.length > 0 ? rows[0].id : null);
      } catch (err) {
        if (err.name === "AbortError") return;
        setError(err.message || "Unable to load reviews.");
      } finally {
        // StrictMode aborts the first effect run; don't clear loading for it.
        if (!controller.signal.aborted) setLoading(false);
      }
    }

    load();

    return () => controller.abort();
  }, [reloadToken]);

  // Browse mode falls back to the newest row when nothing is explicitly
  // selected, which keeps the original "open on the latest PR" behavior.
  // Manual mode never falls back: no selection means "show the editor".
  const selected = useMemo(() => {
    const explicit = reviews.find((review) => review.id === selectedId) ?? null;
    if (explicit || mode === "manual") return explicit;
    return reviews[0] ?? null;
  }, [reviews, selectedId, mode]);

  const showEditor = mode === "manual" && !selected;

  function startManualReview() {
    setMode("manual");
    setSelectedId(null);
    setSubmitError(null);
  }

  function backToBrowse() {
    setMode("browse");
    setSelectedId(null);
    setSubmitError(null);
  }

  function selectReview(id) {
    setMode("browse");
    setSelectedId(id);
  }

  async function submitManualReview(event) {
    event.preventDefault();
    if (submitting || !manualCode.trim()) return;

    setSubmitting(true);
    setSubmitError(null);

    try {
      const response = await fetch("/api/reviews/manual", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code_snippet: manualCode }),
      });

      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(
          typeof body?.detail === "string"
            ? body.detail
            : `Request failed with status ${response.status}`
        );
      }

      const created = await response.json();

      setReviews((rows) => [created, ...rows.filter((row) => row.id !== created.id)]);
      setSelectedId(created.id);
      setManualCode("");
    } catch (err) {
      setSubmitError(err.message || "Unable to submit code for review.");
    } finally {
      setSubmitting(false);
    }
  }

  const newManualReviewButton = (
    <button
      type="button"
      onClick={startManualReview}
      className="shrink-0 whitespace-nowrap rounded border border-slate-200 bg-white px-2 py-1 text-sm text-slate-700 hover:bg-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600"
    >
      New Manual Review
    </button>
  );

  const countSummary = useMemo(() => {
    if (!selected || selected.status !== "reviewed") return null;

    const parts = [
      [selected.critical_count, "critical"],
      [selected.warning_count, "warning"],
      [selected.nitpick_count, "nitpick"],
    ]
      .filter(([count]) => count > 0)
      .map(([count, label]) => `${count} ${label}`);

    return parts.length > 0 ? parts.join(" · ") : "no findings";
  }, [selected]);

  return (
    <div className="flex h-screen flex-col bg-slate-50 font-sans text-slate-900 antialiased">
      <nav className="flex h-12 shrink-0 items-center justify-between border-b border-slate-200 bg-white px-4">
        <span className="text-sm font-medium text-slate-900">CodeRev Bot</span>
        <a
          href={REPO_URL}
          target="_blank"
          rel="noreferrer"
          className="text-sm text-slate-500 underline-offset-2 hover:text-slate-900 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600"
        >
          GitHub
        </a>
      </nav>

      <main className="min-h-0 flex-1 p-4">
        {loading ? (
          <div className="flex h-full items-center justify-center gap-2">
            <Spinner />
            <span className="text-sm text-slate-500">Reviewing code diff...</span>
          </div>
        ) : error ? (
          <div className="flex h-full flex-col items-center justify-center gap-3">
            <p className="text-sm text-slate-500">{error}</p>
            <button
              type="button"
              onClick={() => setReloadToken((token) => token + 1)}
              className="rounded bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600 focus-visible:ring-offset-2"
            >
              Retry
            </button>
          </div>
        ) : reviews.length === 0 && mode === "browse" ? (
          <div className="flex h-full flex-col items-center justify-center gap-3">
            <p className="text-sm text-slate-500">
              No reviews yet. Open or update a pull request to trigger one, or submit
              code manually.
            </p>
            {newManualReviewButton}
          </div>
        ) : (
          <div className="grid h-full min-h-0 grid-cols-1 gap-4 lg:grid-cols-2">
            <Pane
              title={showEditor ? "Code" : "Diff"}
              action={
                <div className="flex min-w-0 items-center gap-3">
                  {mode === "manual" ? (
                    <button
                      type="button"
                      onClick={backToBrowse}
                      className="shrink-0 whitespace-nowrap text-sm text-slate-500 underline-offset-2 hover:text-slate-900 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600"
                    >
                      ← Back to PR reviews
                    </button>
                  ) : null}
                  {reviews.length > 0 ? (
                    <select
                      value={selected?.id ?? ""}
                      onChange={(event) => selectReview(Number(event.target.value))}
                      aria-label="Select a review"
                      className="min-w-0 max-w-xs truncate rounded border border-slate-200 bg-white px-2 py-1 text-sm text-slate-700 focus:border-slate-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600"
                    >
                      {selected ? null : (
                        <option value="" disabled>
                          Select a review
                        </option>
                      )}
                      {reviews.map((review) => (
                        <option key={review.id} value={review.id}>
                          {reviewLabel(review)}
                        </option>
                      ))}
                    </select>
                  ) : null}
                  {newManualReviewButton}
                </div>
              }
            >
              {showEditor ? (
                <ManualEditor
                  code={manualCode}
                  onChange={setManualCode}
                  onSubmit={submitManualReview}
                  submitting={submitting}
                  error={submitError}
                />
              ) : (
                <DiffView diffText={selected?.diff_text} />
              )}
            </Pane>

            <Pane
              title="Review"
              action={
                countSummary ? (
                  <span className="text-xs text-slate-500">{countSummary}</span>
                ) : null
              }
            >
              {selected ? (
                <>
                  <div className="border-b border-slate-200 px-4 py-3">
                    {isManualReview(selected) ? (
                      // Manual rows have pr_url "N/A"; don't render a dead link.
                      <>
                        <p className="text-sm text-slate-900">{selected.repo_name}</p>
                        <p className="mt-0.5 text-xs text-slate-500">
                          {formatTimestamp(selected.created_at)}
                        </p>
                      </>
                    ) : (
                      <>
                        <a
                          href={selected.pr_url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-sm text-slate-900 underline-offset-2 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600"
                        >
                          {selected.pr_title}
                        </a>
                        <p className="mt-0.5 text-xs text-slate-500">
                          {selected.repo_name} #{selected.pr_number}
                        </p>
                      </>
                    )}
                  </div>
                  <ReviewView review={selected} />
                </>
              ) : submitting ? (
                <div className="flex h-full items-center justify-center gap-2">
                  <Spinner />
                  <span className="text-sm text-slate-500">Reviewing code diff...</span>
                </div>
              ) : (
                <p className="p-4 text-sm text-slate-500">
                  {showEditor
                    ? "Submit code to see the review here."
                    : "Select a pull request."}
                </p>
              )}
            </Pane>
          </div>
        )}
      </main>
    </div>
  );
}
