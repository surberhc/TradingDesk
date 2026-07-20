# Newsletter PDF archive moved off the repo — 2026-07-20

The InvesTech newsletter source PDFs (`2023/`, `2024/`, `2025/`, `2026/` — 42 files,
~150 MB) now live at:

    C:\TradingDesk-Local\investech-source\2023\...
                                          \2024\...
                                          \2025\...
                                          \2026\...

Directory structure preserved. Copied with robocopy, then every file verified by
SHA256 against its source (42/42 identical) before anything was deleted.

## Why

- Git deliberately excludes bulk PDFs (`.gitignore`), so sitting in `C:\TradingDesk\`
  they were tracked by nothing — absent from the nightly git bundle.
- They were also outside `C:\TradingDesk-Local\`, so the verified rclone data backup
  did not cover them either. Net: 150 MB of licensed source material with zero backup.
- Moving them under `TradingDesk-Local\` puts them inside the data backup's scope, so
  they are protected — without inflating every nightly git bundle roughly 4.5x, which
  is what committing them would have cost.

## Nothing referenced them

Grepped every `.py` / `.cmd` / `.md` under `C:\TradingDesk\` before the move: no code,
script, or config points at the year folders. The only mention anywhere is the shelved
note in the root `README.md`. The PDFs were reference material read by hand, not an
input to the feed.

The code and the small `_dataset\` reference data the phase2_feed loads by path are now
committed to git, so the `investech\` tree is covered end to end: code in the bundle,
bulk source in the data backup.
