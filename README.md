## v1.1.119 — Accept available native video queue

Skip missing or unreadable entries without a settlement wait or JSON-RPC retrieval fallback. Continue with available native items when sizes change. Save actual playback order and position; clear old video resume if the new queue cannot be captured.

## v1.1.118 — Immediate native queue verification

Remove the XSP settlement wait. Read the native video queue once immediately after Playlist.Add; skip missing IDs without retries or JSON-RPC retrieval fallback. Keep native size consistency checks.

## v1.1.81 — Remove test artifacts

Native XSP queue loading, random ordering and exact restoration remain unchanged. Diagnostics are logged to kodi.log only. The standalone XSP test and its settings button have been removed. Existing files are not automatically deleted.

### v1.1.55 — Unified resume data (no migration)
- All persistent *resume state* is now stored in **`resume data.json`** in the add-on's profile directory, with separate `video`, `music`, and `last_used` sections.
- Saves use atomic replacement and a thread lock to prevent a concurrent music/video/priority save from overwriting another section.
- Older `playlist items.txt`, `playlist position.txt`, `resume point.txt`, `resume item.json`, `music resume.json`, and `last used media.json` are **not imported or read**. They are left on disk untouched for safety; new snapshots are created when media plays.
- The music M3U is only a temporary loader input and is removed after loading. The video queue guard and Play Random smart-playlist cache remain temporary operational files, not resume snapshots. Kodi still stores add-on settings separately as part of its own settings system.
- Existing playback, priority, notification, and shutdown-aware behavior from v1.1.54 is retained.

### v1.1.37 – Music queue resume and media priority
- Save Kodi audio playlist (playlist ID 0) independently of existing video resume files (playlist ID 1).
- Save the complete music queue, exact song, playlist index, and elapsed time to `music resume.json` in the add-on profile. Update it periodically, on starting a track, pausing, or seeking.
- Restore the complete audio queue before opening the player; start at the saved song and seek to the saved time (minus the configured resume offset).
- New **Restore priority** setting under Resumer: **Video first** (default) or **Music first**. When the selected type has no usable saved queue, try the other. This choice applies at startup and wake.
- Honor the existing **Auto-Resume playback at startup**, **Delay resume on start**, **Resume point offset**, and **Resume even if stopped** settings for both media types.
- Prevent partial or inconsistent audio playlists from overwriting a good music snapshot, verify restored tracks before seeking, and disable shuffle during restoration.
- Music playback no longer changes video resume state. Play Random and video playlist logic remain unchanged.
- Music resume applies to audio actually played by Kodi; external player queues are not supported. The new music file will only exist after playing music with this version installed.

### v1.1.35
- Fix Auto Play Random indefinite busy spinner when no eligible items, a filtered-out TV show, or JSON-RPC errors.
- Use bounded random selection (no recursion), one cached smart-playlist exclusion lookup, and guaranteed busy dialog cleanup.
- Preserve complete-queue-before-play behavior required for reliable playlist resume.
- Add visible failure notices and log messages; use 50-item queue batches.

# Kodi Playlist Resumer

- Runs as a service and will periodically update the resume point for an individual file or the entire playlist if items are in Kodi library.
- Will automatically resume a video if Kodi was shutdown while playing it. 
- Can also auto play random video library item if there is nothing to resume.
  - See settings to configure how often the resume point is set, and whether to automatically resume.
  - Adjustable resume point offset
  - Adjustable resume delay

## v1.1.32: Faster Play Random

- Start selected movie/episode playback before fetching random queue items.
- For shows and seasons, fetch the first random episode with a small query before filling the queue.
- Add queued videos via batched Kodi JSON-RPC Playlist.Add calls (200 per batch).
- Cache smart-playlist exclusion membership for 120 seconds to avoid rescanning every click.
- Skip requesting the unused file property when querying random library items.
- Use batch additions in auto-play random as well.

Smart-playlist changes may take up to two minutes to be reflected in Play Random.

## v1.1.33 playlist resume correction

Build the complete playlist before starting playback for manual Play Random, automatic Play Random, and startup playlist resume. This prevents playback-start snapshots from saving only the first video. Batch additions remain enabled; failed batch requests fall back to adding each item individually.

## v1.1.34 resume correction

- Save the precise Kodi movie, episode, or music-video ID alongside the playlist position, and use it on resume.
- Find the currently playing item in the queue by ID when saving the position.
- Restore the entire remaining queue before starting playback, explicitly disable extra shuffle when resuming, and verify the started item before seeking.
- Keep the previous good snapshot if a playback-start event returns an inconsistent queue.
- Existing saved snapshots from older versions lack the exact video ID and still use the saved numeric position.

### Optional fullscreen progress skin integration (1.1.131)

The native background progress dialog is skin-dependent. For a noninteractive
fullscreen bar, add the following group inside the existing `<controls>` element
of your skin's `VideoFullScreen.xml`. Back up the skin file first; no skin files
are modified automatically. Coordinates assume a 1920x1080 skin.

```xml
<control type="group">
    <visible>!String.IsEmpty(Window(Home).Property(KPR.RandomProgress.Active))</visible>
    <left>1280</left><top>45</top>
    <control type="label">
        <left>0</left><top>0</top><width>580</width><height>45</height>
        <label>$INFO[Window(Home).Property(KPR.RandomProgress.Message)]</label>
        <font>font13</font><textcolor>white</textcolor>
    </control>
    <control type="progress">
        <left>0</left><top>48</top><width>580</width><height>16</height>
        <info>Window(Home).Property(KPR.RandomProgress.Percent)</info>
        <texturebg>progress_back.png</texturebg>
        <midtexture>progress_mid.png</midtexture>
    </control>
</control>
```
Adjust font and texture names to match the active skin. Reload the skin after editing.

## v1.1.132 stability diagnostic
Manual random only: no native progress dialog or progress properties, no final GUI refresh, two seconds of stabilization after first playback and 500 ms before each later batch. Busy dialog closes are timed. Resume and automatic random retain their existing behavior.

## v1.1.133
Manual random restores native DialogProgressBG with per-batch updates and timed GUI calls. No custom fullscreen skin properties are updated. All explicit playlist GUI refresh scheduling and execution are removed, including resume and automatic random. Batch pacing and playback stabilization remain unchanged. Native progress visibility depends on the skin.

## v1.1.134
Manual random uses at most two XSP batches: first 100, then all remaining items after verified playback and initial snapshot. Automatic inclusion/exclusion rules per batch; no artificial waits. Progress updates once per completed batch. Explicit playlist refreshes remain disabled.

## v1.1.135
Manual progressive random no longer saves initial, final, or interrupted queue snapshots inline. Existing video snapshot invalidation before replacement is retained. Normal playback tracking retries after the loading marker clears; while loading, tracking skips file details and queue reads. Two batches, progress bar, and no forced playlist refreshes remain unchanged.
