# History

## 2026-10-03

### Native Codex and combined-provider cleanup

- Extended the existing review/plan/apply/verify engine with native Codex readers,
  exact desktop SQLite/prompt-history mirrors, and one plan covering both apps.
- Preserved Codex transcript byte positions and logical SQLite records. Unknown
  formats, active database sidecars, nonempty queues and unproven rich references
  refuse cleanup; generated/internal context remains excluded.
- Independent review, 98 synthetic tests and fresh installed execution passed.
  Read-only compatibility checks accepted current native transcript formats;
  real personal-history cleanup and native-app resumption were not exercised.

### Initial local Claude Code cleanup release

- Added conservative candidate discovery, contextual skill review, exact copy plans,
  dry runs, approved local text redaction and verification.
- Preserved unselected bytes and conversation structure; refused unsupported,
  active, unsafe or stale sources. Incomplete writes and cleanup report truthfully.
- Documented Python 3.10+ macOS/Linux installation and exclusions, with no cloud,
  backup, assistant-quotation or secure-disk-erasure claims.
- Independent checks passed 36 synthetic tests on Python 3.12 and 3.14 plus fresh
  installed execution. Configured GitHub Actions for Linux/macOS and 3.10/3.13.
