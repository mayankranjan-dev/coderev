/**
 * CodeRev Bot — two-pane review workspace.
 *
 * Fetches GET /api/reviews once on mount but never auto-selects a row: the
 * app always opens on the zero-state landing screen, and a PR/manual review
 * only appears once the user explicitly picks one. The dropdown selection
 * filters the already-fetched array. Manual mode POSTs a snippet to
 * /api/reviews/manual, prepends the returned row to that same array, and
 * renders it through the same DiffView/ReviewView path as webhook PRs.
 *
 * Accent choice: indigo-600 is used for primary actions only (review submit,
 * retry, focus rings). Everything structural is slate. The execution
 * sandbox's Terminal is the one deliberate exception — it's styled as a real
 * dark terminal (slate-900/950) to read as an authentic execution surface
 * against the otherwise light, matte workspace.
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

/*
 * Small line-icon set for empty/landing states. Deliberately plain: single
 * stroke, no fill, no gradient — decoration that fits the matte theme rather
 * than fights it.
 */

function CodeIcon(props) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      <path d="M8 4 3 12l5 8" />
      <path d="M16 4l5 8-5 8" />
    </svg>
  );
}

function DocumentIcon(props) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      <path d="M8 3.5h6.5L19 8v11.5a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1Z" />
      <path d="M14.5 3.5V8H19" />
      <path d="M9.5 12.5h5M9.5 15.5h5M9.5 18h3" />
    </svg>
  );
}

/**
 * Centered placeholder for a pane with no content yet. Used for the manual
 * editor's un-submitted review pane and for a PR with no stored diff — never
 * a bare line of text.
 */
function EmptyState({ icon, title, subtitle }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 px-6 py-10 text-center">
      <span className="text-slate-300">{icon}</span>
      <p className="text-sm font-medium text-slate-600">{title}</p>
      {subtitle ? <p className="max-w-xs text-xs leading-5 text-slate-400">{subtitle}</p> : null}
    </div>
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
      <EmptyState
        icon={<DocumentIcon className="h-8 w-8" />}
        title="No diff stored"
        subtitle="This pull request has no diff on record."
      />
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

/**
 * Panel shell. Header reads like an IDE tab — small, uppercase, muted label
 * over a hairline rule — rather than a document title, to match the rest of
 * the workspace chrome.
 */
function Pane({ title, action, children }) {
  return (
    <section className="flex min-h-0 min-w-0 flex-col border border-slate-200 bg-white">
      <header className="flex shrink-0 items-center justify-between gap-4 border-b border-slate-200 px-4 py-2.5">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-500">{title}</h2>
        {action}
      </header>
      <div className="min-h-0 flex-1 overflow-auto">{children}</div>
    </section>
  );
}

/**
 * Read-only output pane for POST /api/execute.
 *
 * Styled as an authentic dark terminal — bg-slate-900 body, bg-slate-950
 * header bar — the one deliberate break from the light workspace chrome,
 * because a sandbox output stream reads as more credible when it looks like
 * one. Flat fills only, still no gradients/shadows/radius.
 *
 * `output` carries status text and stdout; `stderr` is rendered separately in
 * a dim red underneath it, because a run can legitimately produce both (a
 * script that prints, then raises). red-400 rather than red-600 is used for
 * legibility against the dark body — red-600 loses contrast on slate-900.
 */
function Terminal({ output, stderr }) {
  const hasContent = Boolean(output) || Boolean(stderr);

  return (
    <section
      aria-label="Terminal"
      className="flex min-h-0 shrink-0 basis-[30%] flex-col bg-slate-900"
    >
      <header className="flex shrink-0 items-center bg-slate-950 px-4 py-1.5">
        <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
          Terminal
        </span>
      </header>

      <div
        // aria-live so screen readers announce results; the pane is never
        // focusable or editable, matching the read-only requirement.
        aria-live="polite"
        className="min-h-0 flex-1 overflow-auto px-4 py-3"
      >
        {hasContent ? (
          <pre className="whitespace-pre-wrap break-words font-mono text-sm leading-5 text-slate-50">
            {output}
            {output && stderr ? "\n" : null}
            {stderr ? <span className="text-red-400">{stderr}</span> : null}
          </pre>
        ) : (
          <p className="font-mono text-sm leading-5 text-slate-500">
            Run the code to see output here.
          </p>
        )}
      </div>
    </section>
  );
}

function ManualEditor({
  code,
  onChange,
  onSubmit,
  onRun,
  submitting,
  error,
  terminalOutput,
  terminalError,
  isExecuting,
}) {
  const hasCode = code.trim().length > 0;
  const busy = submitting || isExecuting;
  const canSubmit = !busy && hasCode;
  const canRun = !busy && hasCode;

  return (
    <form onSubmit={onSubmit} className="flex h-full min-h-0 flex-col">
      <div className="flex min-h-0 basis-[70%] flex-col gap-3 p-4">
        <label htmlFor="manual-code" className="sr-only">
          Code to review
        </label>
        <textarea
          id="manual-code"
          value={code}
          onChange={(event) => onChange(event.target.value)}
          spellCheck={false}
          placeholder="Paste code to review"
          className="min-h-0 flex-1 resize-none border border-slate-200 bg-white p-3 font-mono text-sm leading-6 text-slate-900 placeholder:text-slate-400 focus:outline-none focus:ring-1 focus:ring-slate-400"
        />
        {error ? (
          <p role="alert" className="text-sm text-red-700">
            {error}
          </p>
        ) : null}
        <div className="flex shrink-0 justify-end gap-2">
          {/* type="button": this must not submit the review form. */}
          <button
            type="button"
            onClick={onRun}
            disabled={!canRun}
            className="border border-transparent bg-slate-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-slate-500 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-slate-700"
          >
            {isExecuting ? "Running..." : "Run Code"}
          </button>
          <button
            type="submit"
            disabled={!canSubmit}
            className="border border-transparent bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-indigo-600"
          >
            Submit for Review
          </button>
        </div>
      </div>

      <Terminal output={terminalOutput} stderr={terminalError} />
    </form>
  );
}

/**
 * Zero-state landing screen. Shown whenever the app is in browse mode with
 * nothing selected — including the very first paint, since selectedId now
 * starts at null and is never auto-filled from the fetched list.
 */
function Landing({ reviews, onSelectReview, onStartManual }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-8 px-4 text-center">
      <div className="flex flex-col items-center gap-3">
        <span className="flex h-12 w-12 items-center justify-center border border-slate-200 bg-white text-slate-400">
          <CodeIcon className="h-6 w-6" />
        </span>
        <h1 className="text-2xl font-semibold tracking-tight text-slate-900">
          CodeRev Analysis Engine
        </h1>
        <p className="max-w-sm text-sm leading-6 text-slate-500">
          Open a pull request review or run a manual sandbox pass. Findings are
          scored by severity and rendered next to your code.
        </p>
      </div>

      <div className="flex flex-col items-center gap-3 sm:flex-row">
        {reviews.length > 0 ? (
          <select
            value=""
            onChange={(event) => onSelectReview(Number(event.target.value))}
            aria-label="Select a pull request review"
            className="w-72 border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 focus:border-slate-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600"
          >
            <option value="" disabled>
              Select a pull request review…
            </option>
            {reviews.map((review) => (
              <option key={review.id} value={review.id}>
                {reviewLabel(review)}
              </option>
            ))}
          </select>
        ) : null}

        <button
          type="button"
          onClick={onStartManual}
          className="w-72 border border-transparent bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600 focus-visible:ring-offset-2 sm:w-auto"
        >
          New Manual Sandbox Review
        </button>
      </div>

      {reviews.length === 0 ? (
        <p className="text-xs text-slate-400">
          No pull request reviews yet — open or update a PR to trigger one automatically.
        </p>
      ) : null}
    </div>
  );
}

export default function App() {
  const [reviews, setReviews] = useState([]);
  // Starts null and is never auto-filled: the app always opens on the
  // Landing zero-state, and only shows a review once the user picks one.
  const [selectedId, setSelectedId] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [mode, setMode] = useState("browse"); // "browse" | "manual"
  const [manualCode, setManualCode] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);
  // Terminal state for the execution sandbox. stdout/status text and stderr are
  // held apart so stderr can be rendered red while stdout stays standard.
  const [terminalOutput, setTerminalOutput] = useState("");
  const [terminalError, setTerminalError] = useState("");
  const [isExecuting, setIsExecuting] = useState(false);

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
        // Deliberately no auto-select here — landing the user on the latest
        // PR by default is what we're moving away from.
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

  // No fallback to the newest row in either mode: no explicit selection means
  // "show the landing/editor state", full stop.
  const selected = useMemo(
    () => reviews.find((review) => review.id === selectedId) ?? null,
    [reviews, selectedId]
  );

  const showEditor = mode === "manual" && !selected;
  const showLanding = mode === "browse" && !selected;

  function startManualReview() {
    setMode("manual");
    setSelectedId(null);
    setSubmitError(null);
    // Don't carry a previous run's output into a fresh editor session.
    setTerminalOutput("");
    setTerminalError("");
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

  /**
   * POST the editor contents to /api/execute and render the result.
   *
   * The backend returns {stdout, stderr} on both success and failure, so the
   * non-ok branch reads the same shape instead of throwing. A program that
   * exits non-zero is a normal result here, not an error state.
   */
  async function runCode() {
    if (isExecuting || submitting || !manualCode.trim()) return;

    setIsExecuting(true);
    setTerminalOutput("Executing...");
    setTerminalError("");

    try {
      const response = await fetch("/api/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: manualCode }),
      });

      const body = await response.json().catch(() => null);

      if (!body) {
        setTerminalOutput("");
        setTerminalError(`Execution failed with status ${response.status}.`);
        return;
      }

      const stdout = typeof body.stdout === "string" ? body.stdout : "";
      const stderr = typeof body.stderr === "string" ? body.stderr : "";

      // An empty successful run still needs feedback, otherwise the pane looks
      // like nothing happened.
      setTerminalOutput(
        stdout || (stderr ? "" : "Execution finished with no output.")
      );
      setTerminalError(stderr);
    } catch (err) {
      setTerminalOutput("");
      setTerminalError(err.message || "Unable to reach the execution sandbox.");
    } finally {
      setIsExecuting(false);
    }
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
      className="shrink-0 whitespace-nowrap border border-slate-200 bg-white px-2 py-1 text-sm text-slate-700 hover:bg-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600"
    >
      New Manual Sandbox Review
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
            <span className="text-sm text-slate-500">Loading reviews...</span>
          </div>
        ) : error ? (
          <div className="flex h-full flex-col items-center justify-center gap-3">
            <p className="text-sm text-slate-500">{error}</p>
            <button
              type="button"
              onClick={() => setReloadToken((token) => token + 1)}
              className="bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600 focus-visible:ring-offset-2"
            >
              Retry
            </button>
          </div>
        ) : showLanding ? (
          <Landing
            reviews={reviews}
            onSelectReview={selectReview}
            onStartManual={startManualReview}
          />
        ) : (
          <div className="grid h-full min-h-0 grid-cols-1 gap-4 lg:grid-cols-2">
            <Pane
              title={showEditor ? "Code" : "Diff"}
              action={
                <div className="flex min-w-0 items-center gap-3">
                  <button
                    type="button"
                    onClick={backToBrowse}
                    className="shrink-0 whitespace-nowrap text-sm text-slate-500 underline-offset-2 hover:text-slate-900 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600"
                  >
                    ← Back to start
                  </button>
                  {reviews.length > 0 ? (
                    <select
                      value={selected?.id ?? ""}
                      onChange={(event) => selectReview(Number(event.target.value))}
                      aria-label="Select a review"
                      className="min-w-0 max-w-xs truncate border border-slate-200 bg-white px-2 py-1 text-sm text-slate-700 focus:border-slate-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600"
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
                  {mode === "browse" ? newManualReviewButton : null}
                </div>
              }
            >
              {showEditor ? (
                <ManualEditor
                  code={manualCode}
                  onChange={setManualCode}
                  onSubmit={submitManualReview}
                  onRun={runCode}
                  submitting={submitting}
                  error={submitError}
                  terminalOutput={terminalOutput}
                  terminalError={terminalError}
                  isExecuting={isExecuting}
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
                // showEditor is guaranteed true here: selected is null and we're
                // not in the Landing branch, so this only renders for an
                // opened-but-not-yet-submitted manual review.
                <EmptyState
                  icon={<DocumentIcon className="h-8 w-8" />}
                  title="No review yet"
                  subtitle="Submit your code to generate a review here."
                />
              )}
            </Pane>
          </div>
        )}
      </main>
    </div>
  );
}
