## What and why
<!-- What does this change, and which issue does it fix? -->

## How it was tested
- [ ] `uv run ruff check && uv run ruff format --check`
- [ ] `uv run pytest -n auto -m "not hardware and not slow and not integration"`
- [ ] covering slow tests (integration / emulated / ui) - which:
- [ ] on a real device - which model:

## Checklist
- [ ] a new device/library/processor passes the contract suites (`tests/contracts/harness.py`)
- [ ] UI strings go through `i18n.js`; screenshots at 1280 px and 390 px attached for UI changes
- [ ] user docs updated (`docs/`)
