/**
 * CodeRev Bot — two-pane review workspace.
 *
 * Fetches GET /api/reviews once on mount. The dropdown selection filters the
 * already-fetched array; no further network calls are made.
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
    "Review pending — the local model was unreachable when this pull request arrived.",
  reviewed: null,
};

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

export default function App() {
  const [reviews, setReviews] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [reloadToken, setReloadToken] = useState(0);

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

  const selected = useMemo(
    () => reviews.find((review) => review.id === selectedId) ?? null,
    [reviews, selectedId]
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
        ) : reviews.length === 0 ? (
          <div className="flex h-full items-center justify-center">
            <p className="text-sm text-slate-500">
              No reviews yet. Open or update a pull request to trigger one.
            </p>
          </div>
        ) : (
          <div className="grid h-full min-h-0 grid-cols-1 gap-4 lg:grid-cols-2">
            <Pane
              title="Diff"
              action={
                <select
                  value={selectedId ?? ""}
                  onChange={(event) => setSelectedId(Number(event.target.value))}
                  aria-label="Select a pull request"
                  className="max-w-[60%] truncate rounded border border-slate-200 bg-white px-2 py-1 text-sm text-slate-700 focus:border-slate-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600"
                >
                  {reviews.map((review) => (
                    <option key={review.id} value={review.id}>
                      {review.repo_name} #{review.pr_number} — {review.pr_title}
                    </option>
                  ))}
                </select>
              }
            >
              <DiffView diffText={selected?.diff_text} />
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
                  </div>
                  <ReviewView review={selected} />
                </>
              ) : (
                <p className="p-4 text-sm text-slate-500">Select a pull request.</p>
              )}
            </Pane>
          </div>
        )}
      </main>
    </div>
  );
}
