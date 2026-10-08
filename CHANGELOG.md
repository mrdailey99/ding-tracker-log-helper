# Log Helper changelog

## 0.15.21

- Removed the alpha version suffix for the packaged release.
- First public helper release, with MIT-licensed source, Windows download,
  setup guide and manual GitHub release workflow.
- Includes the setup guidance, 60-second default interval and Windows tray
  controls introduced in 0.15.21-alpha.
- Available separately or in the combined Windows bundle with Ding Tracker.

## 0.15.21-alpha

- Setup explains exactly where to enter `/ding logs on` and `/reload` in WoW chat.
- Checks default to 60 seconds; choose 5–3600 seconds during setup or with `--interval`.
- Added optional Windows tray mode, with Show/Hide console, Scan now, interval
  choices and Exit helper. Setup defaults to tray mode; console and one-shot
  modes remain available.
- Added `--background`, `--status` and `--stop` for the selected client folder.
- Prevented duplicate background workers for a client and repeated scans or
  errors when files are unchanged. Addon updates replacing Data.lua trigger a
  new import; the helper's own output does not trigger a redundant scan.

## 0.15.20-alpha

- First portable Windows x64 executable; no Python installation needed.
- Optional highest-hit, credited creature-HP and survived-close-call imports
  from Forever 1.60.1 combat logs and the addon's saved context.
