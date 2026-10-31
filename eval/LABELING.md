# How to Label Eval Cases

This is the mechanical procedure for turning a commit into an entry in `eval/cases.yaml`. The rules it applies (which repos, which commits qualify, how to pick the line range, the category list) are in `docs/eval/LABELING_RULES.md` and are the owner's decisions. Where this guide says "per the rules", use whatever the owner decided there; it does not make those decisions.

The case file is label data. Kiro does not write or edit entries (`.kiro/steering/project.md` §7).

## Before you start

1. Answer the open **AUTHOR DECISION** items in `docs/eval/LABELING_RULES.md` (repo list, per-repo cap, sampling and stopping rule, range rules, taxonomy, control rules). Write the answers into that file.
2. Copy `eval/cases.example.yaml` to `eval/cases.yaml`, delete the three fake entries, and set `categories:` to your final taxonomy (or remove the key to allow any category string).
3. Keep a rejection log outside the case file (format up to you): one line per rejected candidate with repo, SHA, and reason. This is the main defense against cherry-picking (`LABELING_RULES.md` §3).

## Bug cases, step by step

For each repo on your list, walk candidates in the order your sampling rule says (for example, first-parent history forward from the cutoff date):

1. **Find a candidate.** `git log --first-parent --since=<cutoff> --reverse --format="%H %cI %s"` in a clone of the repo, or the GitHub commit list. Open the commit and its linked issue or PR.
2. **Check it qualifies** per `LABELING_RULES.md` §4: exactly one parent, committer date on or after the cutoff, changes at least one non-test `.py` file, a behavior bug visible at the parent, localized to one file, within the size limit. If it fails any check, log the SHA and the reason, and move on. Do not skip a qualifying commit.
3. **Record the SHAs.** `fix_sha` is the full 40-character commit SHA. `parent_sha` is its first parent: `git rev-parse <fix_sha>^1`.
4. **Pick the file.** `buggy_file` is the path of the buggy file **at `parent_sha`**, relative to the repo root, with no `a/` or `b/` prefix.
5. **Pick the line range** from the fix commit only, never from model output. Open the parent version (`git show <parent_sha>:<buggy_file>`, or the GitHub "browse files at this commit" view) and apply your range rule (§5.1, §5.2, §5.3). Lines are 1-based and inclusive, and they refer to the **parent** file. A quick check: in `git diff <fix_sha> <parent_sha> -- <buggy_file>`, the buggy lines appear on the `+` side with those numbers.
6. **Pick one category** from your taxonomy (§6).
7. **Give it an id**: short, stable, lowercase, unique (`[a-z0-9][a-z0-9._-]{0,63}`), for example `<repo>-<short-sha>`.
8. **Write the entry** (see the schema at the top of `eval/cases.py`). Use `notes` for a one-line summary of the bug and the issue link.

## Control cases, step by step

1. Walk the same repos and date window (§7).
2. Take single-parent commits that change non-test Python code and are not bug fixes by their message or linked issue. Apply your rule for excluding controls that a later commit fixed or reverted (§7.1) and your size-matching rule (§7.2).
3. Record `parent_sha` and the control commit as `fix_sha` with `split: control`, and no `buggy_file`, lines, or category. (If you decide a different field name for controls in §7.3, the loader in `eval/cases.py` needs a matching change; ask for it.)

## Check the file

```powershell
.\.venv\Scripts\python.exe -c "from eval.cases import load_cases; s = load_cases(); print(len(s.bugs), 'bugs', len(s.controls), 'controls')"
.\.venv\Scripts\python.exe -m eval.fetch
```

The first command checks the schema and lists every problem at once. The second fetches each commit into the gitignored `eval/cache/` and checks the git-side rules: `parent_sha` is the first parent, `fix_sha` is not a merge, `buggy_file` exists at the parent, the range is inside the file, and the file is among the changed `.py` files. It also reports how many cases fall before the contamination cutoff. Fix every reported problem before freezing.

## Freeze

Commit `eval/cases.yaml` before any model sees it (`EVAL_DESIGN.md` §5.4). After the first real run, a label change means a new dataset version and a re-run of every config.
