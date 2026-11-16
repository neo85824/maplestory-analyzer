# MapleStoryAnalyzer

*[繁體中文版 README](README.md)*

A small always-on-top HUD that watches your MapleStory game window and tracks
your HP, MP, EXP, and Level in real time. It uses OCR (optical character
recognition) to read the stat numbers straight off your screen — no typing,
no macros, no game files or memory access. Use it to see how much EXP/HP/MP
you're burning per grinding session, compare sessions, and get a rough ETA
to your next level.

Read-only: it only looks at your screen, it never clicks, types, or sends
anything to the game.

![MapleStoryAnalyzer running alongside the game](docs/screenshot.jpg)

*(HUD shown in Chinese — switch to English any time in Settings.)*

▶️ **[Demo video](https://youtu.be/Joqzcg6g798)** — see it running live.

## Features

- **Live tracking** — LV/HP/MP/EXP update about twice a second, with a
  progress bar for each. A status pill shows Calibrating/Tracking/Idle/
  Paused/Stopped, and a countdown chip shows how long is left in the current
  session. **The app opens Stopped and does not start tracking on launch** —
  hit **Start** to begin a session; the first second or two shows
  "Calibrating..." while it confirms the real max HP/MP and starting EXP
  before counting anything.
- **Pause/Resume** — pause the current session any time. LV/HP/MP/EXP keep
  reading live while paused, but nothing is added to EXP gained or HP/MP
  lost and the timer stops counting down; Resume picks back up from where
  it left off, and whatever happened during the pause isn't counted.
- **Stop** — hit **Stop** while tracking or paused to end the current session
  right away and save it to History, without waiting for the timer.
- **Sessions** — stats reset on a timer (default 10 minutes, adjustable) so
  "EXP diff" always means "since this session started." When the timer ends
  it commits to History and stops by default (toggle off in Settings to go
  back to immediately starting the next session instead). Hit **Restart
  Session** any time to end the current one early — it saves to History by
  default too (also toggleable).
- **History** — every finished session becomes a card: start→end time, EXP
  gained (with %), HP/MP lost (approximate — see Troubleshooting). Newest
  session is always at the top. Click a card's title to give it a custom name
  (e.g. "Ellinia Forest"), or the "×" in its top-right corner to delete it.
- **Settings**:
  - **Window scale** — shrink or grow the whole window with a +/− stepper.
  - **Always on top** — toggle whether the HUD stays above the game.
  - **Language** — switch between 中文 and English any time, instantly.
  - **Session interval** — how often a session auto-resets (1–60 min).
  - **Stop automatically when the timer ends** — on by default; turn off to
    have the timer immediately start the next session instead (the old
    behavior).
  - **Save to History when restarting** — on by default; turn off to have
    Restart discard the current session instead of saving it.
  - **Display toggles** — show/hide HP, MP, EXP, EXP%, level-up ETA, and
    estimated session EXP individually.
- **Level-up ETA** — once a session has a few seconds of data, estimates
  time-to-next-level from your current EXP rate.
- **Estimated session EXP** — projects "at this rate, the whole session will
  end up earning about this much" from the current EXP rate.

## Requirements

- **Windows** — real screen capture needs a live Windows desktop; this
  won't run for real on macOS/Linux.
- MapleStory installed and running.
- **Game UI: the new bottom status bar (2026-09 revamp) is supported.** The old
  UI is no longer supported.
- **Game resolution: verified at 1366×768.** The status-bar crop boxes are
  anchored to the window edges (they don't scale with resolution), so any of
  MapleStory's fixed resolution options should work — but only 1366×768 has
  been verified.

## Install

No Python or setup needed — just the `MapleStoryAnalyzer` folder containing
`MapleStoryAnalyzer.exe`. Put it wherever you like (e.g. `Desktop\MapleStoryAnalyzer`)
and keep the folder intact — the .exe needs the files alongside it.

## Launch tutorial

1. Have MapleStory running and visible (doesn't need to be focused *before*
   you launch the HUD, just focused *while* it tracks — see Troubleshooting).
2. Double-click `MapleStoryAnalyzer.exe`. Windows may show a SmartScreen
   warning on first run since it isn't code-signed — click **More info →
   Run anyway**.
3. A small window titled "MapleStoryAnalyzer" opens, always on top, on the
   **Live** tab by default. If the game window is found and focused, LV/HP/MP/EXP
   should start filling in within a second or two.
4. Click into the game and play normally — the HUD keeps reading in the
   background. Switch to the **History** tab any time to see past sessions,
   or **Settings** to adjust scale, language, session length, etc.
5. Close the window like any other app when you're done — nothing needs to be
   shut down separately.

## Run with Python instead

Prefer running from source instead of the .exe? You'll need:

- **Python 3.10** (via the `py` launcher, e.g. `py -3.10`).
- MapleStory installed and running (same as above).

Open a terminal in this project's folder and run once:

```powershell
py -3.10 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python .venv\Scripts\pywin32_postinstall.py -install
```

This creates a `.venv` folder with everything the app needs (OCR engine, UI
toolkit, screen-capture libraries). You only need to do this once; after
that, launch with:

```powershell
.venv\Scripts\python scripts\run_overlay.py
```

Same Launch tutorial and Troubleshooting apply either way — the app behaves
identically whether started via the .exe or this command.

## Troubleshooting

- **Status pill says "Can't read the game bar", or all fields show `--`.**
  The game window isn't focused (or is on a loading screen). This is a hard
  constraint, not a bug: the game itself dims its rendering when it loses
  focus, and that starves the OCR of readable text. After about 3 seconds
  with nothing readable the status pill says so; the numbers still on screen
  are the last ones read. Click back into the game window.
- **The EXP row sometimes holds still for a few seconds.** About one EXP
  read in six is misread by OCR (a dropped or swapped digit). Each read is
  checked against the level's total EXP, worked out from many reads; one
  that doesn't fit is skipped and the last good value stays up until the
  next good read. This keeps the EXP percentages and the level-up ETA from
  being thrown off by a single bad read.
- **Status pill says "Game window not found."** The HUD looks for a window
  titled `新楓之谷` by default. If your client's window title is different,
  this won't match it.
- **Status pill says "Stat panel is covered."** Another window is sitting over
  the game's bottom LV/HP/MP/EXP strip. The app reads that area off the
  screen, so whatever is on top would be read instead of your stats — it stops
  and waits rather than recording nonsense. Move the window (or the HUD) off
  that strip and tracking resumes within a couple of seconds. The countdown
  timer keeps running the whole time it's covered, and still ends and
  commits to History on schedule — it doesn't just stall because it can't
  read the screen.
- **The stat bar is hidden behind the Windows taskbar.** With the game
  maximised, the bottom stat strip can sit underneath the taskbar. Run the game
  windowed, or set the taskbar to auto-hide.
- **Status pill says "Game window minimized."** Restore the game window (it
  doesn't need to be the foreground window once tracking has started again,
  just not minimized).
- **A field is occasionally wrong for one tick, then corrects itself.**
  OCR misreads happen — usually caused by a combat effect or floating damage
  number covering a stat bar for a frame. The HUD carries forward the last
  known good value instead of flashing blank, so a single bad tick shouldn't
  be visible.
- **HP/MP loss looks too high, too low, or plain wrong.** Treat HP/MP loss as
  a rough figure, not an exact count. Two reasons. *Sampling:* the HUD reads
  the bars twice a second and adds up the drops between consecutive reads, so
  damage you heal back inside one 500 ms gap is never seen at all, and several
  hits landing in the same gap count as one. *OCR:* HP/MP loss is a running
  total, so a single misread digit (824 read as 24) adds a large phantom loss
  that stays in the session total for good. EXP doesn't have this problem —
  it's just end minus start, so a bad reading in the middle corrects itself on
  the next tick. If a session's HP/MP looks absurd, hit **Restart Session** to
  start the count over.
- **Numbers look frozen / EXP not moving even though I'm playing.** Check the
  status pill — if it says "Tracking," data is flowing; if HP/MP/EXP truly
  haven't changed, that's genuinely idle (not a bug). If the pill shows an
  error instead, see the two bullets above.
- **Text is too small/cramped, or the window is an awkward size.** Settings
  → Window Scale, use the +/− stepper. There's also a scrollbar in Settings
  if some options are cut off at very small scales.
- **Want it to stop covering the game.** Settings → turn off "Always on top,"
  or just move/resize the window like any other.
- **EXP occasionally pauses for a second before updating.** This is by design:
  each reading is checked for plausibility, and a frame that fails is discarded
  in favour of the last good value until the next frame reads correctly. Half a
  second of staleness beats showing a wrong number.
- **Session numbers look "off" after clicking Restart quickly twice.** By
  design, a restart within 1 second of the previous one is ignored (avoids
  logging a meaningless 0-duration entry) — this is expected, not a bug.

