# Add a library

A library type is one class in `syncmusic/libraries/` plus one line in `LIBRARIES` of `syncmusic/core/registry.py`.

## The class
Subclass `Library` (`syncmusic/base/library.py`) and give it:
- `type_name`, `display_name`, a Pydantic `Settings` model (credentials as `SecretStr` - they are masked in the API,
  logs and the diagnostics bundle) and `capabilities: LibraryCapabilities` (which kinds - music, podcasts,
  audiobooks - and whether it takes ratings, plays, positions, playlists, favourites back);
- `check()` → health (reachable, logged in); `state_token()` → a cheap string that changes whenever the content
  changes (the planner skips unchanged libraries);
- `snapshot()` → `LibrarySnapshot` with every item as a `Track` (stable `item_id`, tags, duration, size, format);
- `open_media(item_id)` → a local `Path` or an async byte stream; `open_cover`, `open_thumb`, `get_lyrics` when the
  server has them; `close()`.

A library that takes listening data back also implements `FeedbackSink` (`syncmusic/base/feedback.py`): it receives
plays, ratings, favourites and positions from the reverse sync and must be idempotent (the same play delivered twice
counts once).

The simplest example is `libraries/folder.py`; `libraries/subsonic.py` is a full server with feedback. HTTP clients
use `httpx.AsyncClient`; never log URLs with credentials.

## Contract suite
In `tests/contracts/harness.py` add a factory that returns the library with at least two items (for a server: a fake
in `tests/fake_*.py` served through `httpx.MockTransport`), a `mutate` function that changes the content, and
optionally `bad_credentials`. Entry in `LIBRARIES`; a `FeedbackSink` also gets an entry in the feedback harness.
`uv run pytest tests/contracts -k <type>` runs the suites. Integration tests against the real server go into
`tests/integration/` with the service in `tests/integration/compose.yaml` (with a `mem_limit`).
