# Changelog

## 3.4.0

**New**
- **Speed tab** (next to Timing and Beat FX): change the speed of the exported video — any value from
  0.25× to 4×, typed or dragged. Picture, song and the video's own sound speed up or slow down together
  and stay on the beat; beat effects, the beat LEDs and the BPM follow. The Synced audio and Full song
  downloads use the same speed. A nudge at any speed is as fast as at 1×.
- **Pitch switch in the studio** (Speed tab, and still in the sidebar — they are the same setting):
  locked keeps the song's key at any speed; off lets pitch follow speed like a turntable.
- **Player controls on the video**, like a browser player but native: play / pause, seek, time,
  playback speed and full screen. A round play button shows on a paused video. Click to play / pause,
  double-click for full screen. The controls hide while playing and come back when you move the mouse.
- **Full screen** with the same controls (Esc or F to leave, ← / → to skip 5 s, Space to play / pause).
- **Playback speed** (0.25× – 2× or any custom value) for watching the preview only — it is never
  exported. The key is kept where Qt supports it.

**Fixed**
- The preview's sound stayed on the device that was the default when BeatSync opened (e.g. the monitor)
  after switching Windows to headphones. It now follows the Windows default output device.

## 3.3.0

A native app: the window is now built with Qt instead of a web page.

**Changed**
- No HTML, no browser component and no local web server: the window is native (Qt for Python) and
  the engine runs inside the app. The Microsoft Edge WebView2 Runtime is no longer needed.
- Opens compact — 900×620, centred (smaller if the screen is) — and can be maximised.
- New layout: video, song, match settings and the Match button in a sidebar that is always visible;
  the preview with Timing / Beat FX / Color / Frame & fade / Export tabs on the right; Download in a
  bar along the bottom. Mix and Pitch now live in one place and change the result live.
- Drop a video and a song anywhere in the window — each goes to the right slot.
- Sessions no longer expire while the window is open.
- `--selftest` checks a build (engine, window, video playback) and is run by the release workflow.

**Fixed**
- The preview could go blank after saving a file (a WebView2 issue — gone with the native window).

## 3.2.4

**Changed**
- The window opens at a comfortable size (about 1000×720, centred) instead of almost filling the
  screen. On small or high-scaling screens (e.g. a 1080p laptop at 150 %) it shrinks to fit.
  It can still be maximised.
- Reorganised for smaller windows: a one-line header; video and song on the left, match settings
  and the Match button on the right; after matching, the preview and Download stay on the left
  (always in view) and the timing / effects / color / export controls sit on the right.
  Narrow windows fall back to one column.

## 3.2.3

**Fixed**
- The preview went blank after saving a file, until a nudge or setting change. The preview is
  now reloaded right after the Save-as dialog, after "Show in folder", and when the window comes
  back from being minimised — same spot, same play state.

## 3.2.2

**Fixed**
- Download said "not found" after matching another video, until a nudge or setting change.
  A download now always works: if the finished file is missing, the app serves the newest
  finished version or quietly rebuilds it (same result, usually a few seconds).
- Starting a new match now clears the old download buttons, so the old (deleted) session can
  never be requested, and a late change from the old session can't replace the new one.
- The app window can no longer open a download link by itself (which showed an error page).
- Clearer messages: "Still exporting…" while the export runs, and "This session has expired"
  when it really has.

**Changed**
- Saving never asks to replace an earlier file: the Save-as dialog suggests
  `syncedVid (1).mp4`, `syncedVid (2).mp4` … when the name is taken, and opens in the folder
  you saved to last.

## 3.2.1

Much faster with big video files, with the same beat analysis as 3.2.0 and no loss of quality.

**Faster**
- Beat nudges, pitch and mix changes no longer re-encode the video. Each look (filters, effects,
  aspect, speed, quality) is encoded once, and later changes only swap the audio: seconds instead
  of minutes. Beat effects still re-render on a nudge, because they move with the beat.
- The full export starts in the background while the audio is analysed and the preview renders.
  A new adjustment joins that encode instead of restarting it.
- Trimming a video no longer re-encodes the whole clip before analysis. The trim is applied
  frame-accurately during the export, so there is one encode instead of two (sharper result).
- H.265/HEVC videos with no picture changes are copied straight into the MP4 (lossless and
  instant). They used to be re-encoded to H.264.
- Desktop: clicking the drop box picks the file in place (no upload, no copy). Saving a finished
  file on the same drive is instant (hard link).
- Beat analysis is faster with identical results: an exact but much faster median filter for the
  harmonic/percussive split, video and song analysed in parallel, and both song versions
  (pitch-locked and turntable) made at the same time.
- The video and the song are downloaded or decoded at the same time.
- Previews of 1080p/4K videos shrink to 720p before colour and effects are applied (unless grain,
  sharpen, denoise, scanlines or glitch are on, which depend on pixel size).
- The full-song MP3 is built after the export (or on download) instead of delaying every export.
- With the song mixed with the video's own audio, that audio is decoded once per session, instead
  of the whole video file being read again on every export.

**Fixed**
- A cmd window opened for every ffmpeg run (adding a video, rendering).
- Space sometimes paused the preview and then it played again: after a click on the player,
  its own controls also reacted to the key. Holding Space a little long also toggled twice.
- Temp files from older adjustments are deleted as you go. Before, every nudge left a full-size
  export on disk until the session ended.
- A failed song download stops the video download straight away and shows the real error.
- Uploading a file with a non-Latin name (Arabic, etc.) no longer fails with "Unsupported file type".
- The temp-size readout no longer counts hard-linked uploads twice.
