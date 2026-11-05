# Add a processor

A processor is one class in `syncmusic/processors/` plus one line in `PROCESSORS` of `syncmusic/core/registry.py`.
Processors form a per-device chain; each sees the work item the previous one produced.

## The class
Subclass `Processor` (`syncmusic/base/processor.py`):
- `type_name`, `display_name`, a Pydantic `Settings` model and `stage` (`audio`, `tags`, `cover` or `sidecar`: the
  chain runs in stage order);
- `plan(item, caps)` - **pure**, no I/O: predict the output (format, size). The planner uses it for space checks, so
  a wrong prediction makes a sync fail late;
- `run(item, caps)` - do the work on the file in the work dir (blocking tools such as ffmpeg through
  `asyncio.to_thread` or `infra/ffmpeg.py`) and return the new `WorkItem`;
- `needs` - library inputs to fetch first (`"cover"`, `"lyrics"`);
- `version` - bump it when the output changes for the same input and settings (the cache key includes it);
- `hash_omit_defaults` - every setting added later goes here, so adding it does not re-process every file already
  on a device;
- `suggest(caps)` - a default slot for new devices whose capabilities need it (e.g. transcode when the device
  cannot play FLAC), or `None`.

Look at `processors/retag.py` (tags) and `processors/transcode.py` (audio) as examples.

## Contract suite
In `tests/contracts/harness.py` add a `ProcessorHarness`: a factory from settings, a `supported` input the processor
changes, optionally an `unsupported` input it must pass through unchanged, `alt_settings` that must change the cache
key, and the allowed size prediction error. `uv run pytest tests/contracts -k <type>` runs it.
