# History

## 2026-10-03

### Initial local Claude Code cleanup release

- Added conservative candidate discovery, contextual skill review, exact copy plans,
  dry runs, approved local text redaction and verification.
- Preserved unselected bytes and conversation structure; refused unsupported,
  active, unsafe or stale sources. Incomplete writes and cleanup report truthfully.
- Documented Python 3.10+ macOS/Linux installation and exclusions, with no cloud,
  backup, assistant-quotation or secure-disk-erasure claims.
- Independent checks passed 36 synthetic tests on Python 3.12 and 3.14 plus fresh
  installed execution. Configured GitHub Actions for Linux/macOS and 3.10/3.13.
