# Security Notes

## Pre-existing gitleaks finding in history

History was not rewritten. If this value is a real credential, rotate it at its provider; it is already public in main's history.

| Field | Value |
|---|---|
| Rule ID | `generic-api-key` |
| File | `README.md` |
| Line | 657 |
| Commit | `1b9c3b64652a3b6e956dd30f317ef67eeae3171c` |
| Masked value | `sk-1...` (first 4 characters), length 19 |
| Fingerprint | `1b9c3b64652a3b6e956dd30f317ef67eeae3171c:README.md:generic-api-key:657` |

### Classification: PLACEHOLDER

Based on local evidence only. No network service was called and the value was not tested.

- It sits in the README "Example Code Snippet to Test" Python block, under the comment `# Hardcoded secret`, next to a deliberate SQL injection sample. The snippet is test input for the review bot.
- After the `sk-` prefix, the value is the keyboard sequence `1234567890` followed by `abcdef` (checked by exact comparison).
- Its length (19) is far shorter than a real provider key with an `sk-` prefix.
- The value appears only on this line in the current tree and was introduced by one commit.

### Handling

- The fingerprint is listed in `.gitleaksignore` so full-history scans (CI) stay green.
- The pre-push hook scans only the commits being pushed, so this old commit no longer blocks pushes.
