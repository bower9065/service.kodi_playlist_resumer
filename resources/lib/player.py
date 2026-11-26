if __package__:
    from . import timing
else:
    import timing

from random import randint
from .common import *
from .store import Store
from .status import with_status
from . import music_resumer, video_queue, library_cache
import json
import time
import threading
import os
import xbmc
from xbmcaddon import Addon

class KodiPlayer(xbmc.Player):
    """
    This class represents/monitors the Kodi video player
    """
    def __init__(self, *args):
        xbmc.Player.__init__(self)
        log('KodiPlayer __init__')
        self._active_media = None
        self._restoring_music = False
        self._preserve_music_on_stop = False
        self._completion_generation = 0
        self._video_queue_position = -1
        self._pending_stop = None
        self._shutdown_seen = False
        self._video_capture_key = None
        self._video_capture_due = 0
        self._video_capture_blocked = False
        self._video_capture_generation = 0

    def onPlayBackPaused(self):
        if Store.kodi_event_monitor and Store.kodi_event_monitor.abortRequested():
            return
        if self.isPlayingAudio():
            music_resumer.capture(self)
            log('Paused an audio track.')
            return
        log('onPlayBackPaused')
        Store.paused_time = time.time()
        log(f'Playback paused at: {Store.paused_time}')

    def onPlayBackEnded(self):  # video ended normally (user didn't stop it)
        if Store.kodi_event_monitor and Store.kodi_event_monitor.abortRequested():
            return
        self.cancel_pending_stop("natural playback end")
        log("onPlayBackEnded")
        if self._active_media != 'music' and video_queue.snapshots_blocked():
            log("Ignoring video end during unfinished queue construction")
            return
        if self._active_media == 'music':
            if music_resumer.should_preserve_on_track_end():
                log('Music track ended: next track remains; keeping resume snapshot')
            else:
                music_resumer.clear()
            return
        self.update_resume_point(-1)
        if not Store.autoplay_queue_completion or Store.was_trailer or Store.was_audio:
            return
        # Kodi can reset the active playlist position before the ended callback.
        playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
        position = self._video_queue_position
        if playlist.size() > 0 and 0 <= position < playlist.size() - 1:
            log('Queue completion autoplay: more videos remain')
            return
        self._completion_generation += 1
        generation = self._completion_generation
        threading.Thread(target=self.autoplay_random_if_enabled,
                         kwargs={'queue_completion': True, 'generation': generation,
                                 'idle_started': (time.monotonic(), xbmc.getGlobalIdleTime())},
                         daemon=True).start()

    def cancel_pending_stop(self, reason):
        if self._pending_stop is not None:
            self._pending_stop = None
            log("KPR STOP: preserving resume snapshot - " + reason, level=xbmc.LOGDEBUG)

    def mark_shutdown(self, reason):
        self._shutdown_seen = True
        self.cancel_pending_stop(reason)
        log("KPR STOP: shutdown detected - " + reason, level=xbmc.LOGDEBUG)

    def onPlayBackStopped(self):
        self._completion_generation += 1
        monitor = Store.kodi_event_monitor
        if self._shutdown_seen or (monitor and monitor.abortRequested()) or Store.just_woke or Store.just_suspend:
            self.cancel_pending_stop("shutdown/sleep/wake")
            log("KPR STOP: preserving resume snapshot - shutdown/sleep/wake", level=xbmc.LOGDEBUG)
            return
        if video_queue.snapshots_blocked() or self._restoring_music or self._preserve_music_on_stop:
            self._preserve_music_on_stop = False
            self.cancel_pending_stop("internal queue operation")
            log("KPR STOP: internal playback stop - preserving snapshot", level=xbmc.LOGDEBUG)
            return
        if Store.resume_if_stopped:
            self.cancel_pending_stop("Resume if Stopped enabled")
            return
        self._pending_stop = (time.monotonic() + 10.0, self._active_media, self._completion_generation)
        log("KPR STOP: playback stopped - awaiting classification for 10 seconds", level=xbmc.LOGDEBUG)

    def process_pending_stop(self):
        pending = self._pending_stop
        if pending is None:
            return
        monitor = Store.kodi_event_monitor
        if self._shutdown_seen or monitor.abortRequested() or Store.just_woke or Store.just_suspend:
            self.cancel_pending_stop("shutdown/sleep/wake")
            return
        if (self.isPlaying() or video_queue.snapshots_blocked() or self._restoring_music
                or pending[2] != self._completion_generation or Store.resume_if_stopped):
            self.cancel_pending_stop("new playback, internal queue operation or setting changed")
            return
        if time.monotonic() < pending[0] or self._pending_stop is not pending:
            return
        self._pending_stop = None
        if monitor.abortRequested() or self._shutdown_seen or Store.just_suspend or Store.just_woke or self.isPlaying() or video_queue.snapshots_blocked() or pending[2] != self._completion_generation:
            log("KPR STOP: late lifecycle change - preserving snapshot", level=xbmc.LOGDEBUG)
            return
        log("KPR STOP: grace period expired - manual stop assumed (media=%s)" % pending[1], level=xbmc.LOGDEBUG)
        if pending[1] == 'music':
            music_resumer.clear()
        else:
            self.update_resume_point(-2)

    def onAVStarted(self):
        if Store.kodi_event_monitor and Store.kodi_event_monitor.abortRequested():
            return
        self._completion_generation += 1
        self.cancel_pending_stop("new playback started")
        log("onAVStarted")
        if self.isPlayingAudio():
            Store.was_audio = True
            self._active_media = 'music'
            Store.record_last_used_media('music')
            if not self._restoring_music:
                music_resumer.capture(self)
            log("Playing audio track - music queue snapshot enabled.")
            return
        Store.was_audio = False
        self._active_media = 'video'
        self._video_queue_position = xbmc.PlayList(xbmc.PLAYLIST_VIDEO).getposition()
        self._video_capture_generation += 1
        self._video_capture_due = 0

    def capture_video_if_due(self):
        """Service-driven tracking; never hold Kodi's playback callback open."""
        if not self.isPlayingVideo():
            self._video_capture_key = None
            self._video_capture_due = 0
            return
        if video_queue.snapshots_blocked():
            self._video_capture_blocked = True
            self._video_capture_key = None
            return
        now = time.monotonic()
        path = self.getPlayingFile()
        position = xbmc.PlayList(xbmc.PLAYLIST_VIDEO).getposition()
        self._active_media = 'video'
        Store.was_audio = False
        self._video_queue_position = position
        Store.was_trailer = '-trailer' in path.lower()
        if Store.was_trailer or Store.is_excluded(path):
            self._video_capture_key = None
            return
        key = (path, position, self._video_capture_generation)
        changed = key != self._video_capture_key or self._video_capture_blocked
        dirty = Store.type_of_video in ('movie', 'episode', 'musicvideo') and Store.video_queue_dirty()
        if not changed and not dirty and now < self._video_capture_due:
            return
        generation = self._video_capture_generation
        total = self.getTotalTime()
        seconds = self.getTime()
        if total <= 0:
            return
        if changed or dirty or not Store.currently_playing_file_path:
            if changed:
                Store.clear_old_play_details(keep_resume_snapshot=True)
                # A new queue can have the same size as the previous queue.
                Store._video_queue_cache = None
            Store.update_current_playing_file_path(path)
            if Store.currently_playing_file_path != path:
                return
        if (video_queue.snapshots_blocked() or not self.isPlayingVideo()
                or self.getPlayingFile() != path or generation != self._video_capture_generation
                or xbmc.PlayList(xbmc.PLAYLIST_VIDEO).getposition() != position):
            self._video_capture_key = None
            return
        Store.length_of_currently_playing_file = total
        Store.record_last_used_media('video')
        self.update_resume_point(seconds)
        snapshot = Store.get_video_snapshot()
        expected_seconds = int(seconds)
        if expected_seconds <= Store.ignore_seconds_at_start or int(expected_seconds * 100 / total) >= 100 - Store.ignore_percent_at_end:
            expected_seconds = 0
        if snapshot.get('seconds') != expected_seconds:
            self._video_capture_due = 0
            return
        self._video_capture_key = key
        self._video_capture_blocked = False
        self._video_capture_due = now + max(1, Store.save_interval_seconds)

    def onPlayBackSeek(self, time, seekOffset):
        if not self.isPlayingAudio():
            self._video_capture_due = 0

    def onPlayBackSeekChapter(self, chapter):
        if not self.isPlayingAudio():
            self._video_capture_due = 0

    def update_resume_point(self, seconds):
        """
        This is where the work is done - stores a new resume point in the local snapshot for the currently playing file

        :param: seconds: the time to update the resume point to.  @todo add notes on -1, -2 etc here!
        :param: Store.library_id: the Kodi library id of the currently playing file
        :return: None
        """
        seconds = int(seconds)
        if video_queue.snapshots_blocked() and not self.isPlayingAudio():
            log("Deferring resume point update while video queue is incomplete")
            return
        if not Store.currently_playing_file_path:
            log("No valid currently_playing_file_path found - therefore not setting resume point")
            return
        if seconds == -2:
            if not Store.kodi_event_monitor.abortRequested():                         
                if Store.resume_if_stopped:
                    log("Video was stopped by user with Resume if Stopped on, and will keep our resume point")   
                    return
                else:
                    log("Video was stopped by user with Resume if Stopped off, and will remove our resume point")                  
                    Store.clear_old_play_details()
                    timing.execute('{"jsonrpc":"2.0","method":"Playlist.Clear","params":{"playlistid":1},"id":"playlist_clear"}')
                    return
            else:
                log("Video was stopped as a result of Abort Requested, and will keep our resume point")   
                return      
        if seconds == -1:
            log("Removing resume points because the file has ended normally")
            Store.clear_old_play_details()
            return         
        if not Store.length_of_currently_playing_file:
            return
        # Preserve Kodi's configured start/end resume rules, including the boundary.
        percent_played = int((seconds * 100) / Store.length_of_currently_playing_file)
        if seconds <= Store.ignore_seconds_at_start or percent_played >= (100 - Store.ignore_percent_at_end):
            seconds = 0
        Store.update_video_snapshot(seconds=seconds)

    @timing.span("player.startup_playback")
    def startup_playback(self, idle_started=None):
        """One shared delay before deciding between resume and random playback."""
        monitor = Store.kodi_event_monitor or xbmc.Monitor()
        if not (Store.resume_on_startup or Store.autoplay_random):
            return
        if monitor.abortRequested() or self.isPlaying():
            return
        idle_started = idle_started or (time.monotonic(), xbmc.getGlobalIdleTime())
        delay = max(0, int(Store.resume_delay))
        deadline = time.monotonic() + delay
        log(f'Startup playback: waiting {delay} seconds before resume decision')

        idle_aborted = False

        def canceled():
            nonlocal idle_aborted
            if idle_aborted:
                return True
            if monitor.abortRequested() or self.isPlaying():
                return True
            if not (Store.resume_on_startup or Store.autoplay_random):
                return True
            elapsed = time.monotonic() - idle_started[0]
            if xbmc.getGlobalIdleTime() + 1 < idle_started[1] + elapsed:
                idle_aborted = True
                log('Startup playback canceled: user activity detected')
                notify('Automatic playback canceled...', xbmcgui.NOTIFICATION_INFO)
                return True
            return False

        while time.monotonic() < deadline:
            if canceled():
                return
            if monitor.waitForAbort(min(0.5, max(0, deadline - time.monotonic()))):
                return
        if canceled():
            return
        if Store.was_trailer:
            Store.was_trailer = False
            return
        log('Startup playback: delay complete; checking saved playback')
        if self.resume_if_was_playing(before_open=lambda: not canceled()):
            return
        if canceled():
            return
        if Store.resume_on_startup and Store.has_saved_resume_snapshot():
            log('Saved resume queue exists but restore failed; suppressing Auto Play Random to protect resume data')
            return
        self.autoplay_random_if_enabled(idle_started=idle_started)

    @timing.span("player.resume_if_was_playing")
    def resume_if_was_playing(self, before_open=lambda: True):
        """Restore preferred media after the shared startup delay."""
        if Store.kodi_event_monitor and Store.kodi_event_monitor.abortRequested():
            return False
        if self.isPlaying() or not Store.resume_on_startup:
            return False
        priority = getattr(Store, 'restore_priority', 'video')
        if priority == 'last_used':
            priority = Store.get_last_used_media()
        log(f"Startup restore priority resolved to: {priority}")
        if priority == 'music':
            return (music_resumer.restore(self, before_open) or (before_open() and self.resume_video_if_was_playing(before_open)))
        return (self.resume_video_if_was_playing(before_open) or (before_open() and music_resumer.restore(self, before_open)))

    def capture_music_if_due(self):
        """Called by the service loop, rather than blocking Kodi's callbacks."""
        if self.isPlayingAudio():
            music_resumer.capture(self, periodic=True)

    def _clear_music_after_video_restore(self, monitor, verified=False):
        """Discard music only for a verified, completed automatic video restore."""
        if (verified and not monitor.abortRequested() and self.isPlayingVideo()
                and Store.get_music_snapshot() is not None):
            music_resumer.clear()
            log('Verified video restored: cleared saved music resume playlist')

    @timing.span("player._wait_for_restored_filepath")
    def _wait_for_restored_filepath(self, expected_file, monitor):
        """Player.play() is asynchronous; a successful call is not proof."""
        for _ in range(50):
            if monitor.abortRequested():
                return False
            if self.isPlayingVideo():
                try:
                    if self.getPlayingFile() == expected_file and self.getTotalTime() > 0:
                        return True
                except RuntimeError:
                    pass
            if monitor.waitForAbort(0.2):
                return False
        log('Video restore: saved filepath did not start; keeping music resume')
        return False

    @timing.span("player.resume_video_if_was_playing")
    @with_status
    def resume_video_if_was_playing(self, before_open=lambda: True, *, status):
        """
        Automatically resume a video after a crash.
        Starts and seeks the saved library item, then restores the remaining XSP queue.
        """

        if self.isPlaying():
            log("Resume stopped: already playing")
            return False

        monitor = Store.kodi_event_monitor or xbmc.Monitor()
        if monitor.abortRequested():
            return False

        snapshot = Store.get_video_snapshot()
        if (not Store.resume_on_startup or not snapshot or
                not snapshot.get('items') or snapshot.get('seconds') is None or
                snapshot.get('position') is None):
            log('Resume stopped: no complete video snapshot in resume data.json')
            return False
        saved_items = snapshot['items']
        if not isinstance(saved_items, (str, list)):
            log('Resume stopped: invalid stored playlist type')
            return False
        if isinstance(saved_items, list) and not saved_items:
            log('Resume stopped: empty playlist')
            return False

        if not before_open():
            return False
        identity = snapshot.get('resume_item') or {}
        saved_target = identity.get('item') if isinstance(identity, dict) else None
        if not saved_target and isinstance(saved_items, list):
            try:
                saved_target = saved_items[int(snapshot['position'])]
            except (IndexError, ValueError, TypeError):
                pass
        is_music_video = isinstance(saved_target, dict) and 'musicvideoid' in saved_target
        resume_heading = "Resume Music Video" if is_music_video else "Resume Video"
        resume_label = "music video" if is_music_video else "video"
        status.update(0, resume_heading, "Preparing to resume %s playlist..." % resume_label)
        if not wait_preparing(monitor, before_open):
            return False
        status.update(0, resume_heading, "Restoring saved %s..." % resume_label)

        # Read the video snapshot from the same JSON object used for music.
        try:
            resume_point = float(snapshot['seconds'])
            if resume_point < 0:
                raise ValueError('negative saved time')
        except (ValueError, TypeError, KeyError):
            log('Invalid saved video resume position')
            xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
            return False

        # ---- FILEPATH PLAYBACK ----
        if isinstance(saved_items, str):
            log('Resuming via filepath')
            if not before_open():
                xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
                return False
            try:
                video_queue.clear_playlist_first(monitor)
            except RuntimeError as error:
                log("Video resume clear failed: " + str(error))
                xbmc.executebuiltin("Dialog.Close(busydialognocancel)")
                return False

            self.play(saved_items)
            verified = self._wait_for_restored_filepath(saved_items, monitor)
            if verified and resume_point > 0:
                try:
                    offset = resume_point - int(Store.resume_offset)
                    self.seekTime(offset if offset > 0 else resume_point)
                except RuntimeError:
                    log("Standalone video seek failed", level=xbmc.LOGWARNING)
            xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
            self._clear_music_after_video_restore(monitor, verified=verified)
            return True

        # ---- PLAYLIST PLAYBACK ----
        items = saved_items
        try:
            position = int(snapshot['position'])
        except (ValueError, TypeError, KeyError):
            log('Invalid saved video playlist position')
            xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
            return False

        if not isinstance(items, list) or not items:
            log("Resume stopped: stored playlist has no playable entries")
            xbmc.executebuiltin("Dialog.Close(busydialognocancel)")
            return False

        # Prefer exact saved library identity over a potentially stale index.
        saved_identity = snapshot.get('resume_item')
        saved_item = (saved_identity.get('item')
                      if isinstance(saved_identity, dict) else None)
        saved_file = (saved_identity.get('file')
                      if isinstance(saved_identity, dict) else None)

        if isinstance(saved_item, dict) and saved_item:
            positions = [i for i, item in enumerate(items) if item == saved_item]
            if not positions:
                log(f"Resume stopped: saved video {saved_item} not in saved playlist; "
                    "refusing to play the wrong file")
                xbmc.executebuiltin("Dialog.Close(busydialognocancel)")
                return False
            position = position if position in positions else positions[0]
            log(f"Resume target matched by ID: {saved_item} at index {position}")
        elif not 0 <= position < len(items):
            log(f"Resume stopped: old stored position {position} invalid "
                f"for {len(items)} saved items")
            xbmc.executebuiltin("Dialog.Close(busydialognocancel)")
            return False

        first_item = items[position]
        # Start the saved item first, then restore the original playlist order.
        queue = items
        playback_verified = [False]
        def seek_saved_video():
            # Seek only once playback is ready.
            waited = 0
            while (
                not self.isPlayingVideo()
                or self.getTotalTime() <= 0
            ) and not monitor.abortRequested() and waited < 50:
                monitor.waitForAbort(0.2)
                waited += 1

            if monitor.abortRequested():
                return
            if self.isPlayingVideo():
                # Playback starting is not enough: verify which video Kodi opened
                # before seeking to the previously saved time.
                playing_item = None
                try:
                    playing_response = json.loads(timing.execute(json.dumps({
                        "jsonrpc": "2.0", "method": "Player.GetItem",
                        "params": {"playerid": 1, "properties": ["file"]},
                        "id": "resume_playing_item"
                    })))
                    playing_item = playing_response.get('result', {}).get('item', {})
                except (ValueError, TypeError):
                    pass

                # A matching identity or filepath must be positively verified
                # before a successful queue build may invalidate saved music.
                # Without the saved resume_item, use the expected first entry.
                expected_item = saved_item if saved_item else first_item
                verified_by_id = False
                if isinstance(expected_item, dict) and isinstance(playing_item, dict):
                    kind = playing_item.get('type')
                    item_id = playing_item.get('id')
                    verified_by_id = (isinstance(kind, str) and
                                      type(item_id) is int and
                                      expected_item.get(kind + 'id') == item_id)
                try:
                    playing_file = self.getPlayingFile()
                except RuntimeError:
                    playing_file = None
                file_matches = bool(saved_file and playing_file == saved_file)
                if not verified_by_id and not file_matches:
                    log(f"Resume skipped seek: expected {expected_item} / {saved_file}, "
                        f"but Kodi opened {playing_item} / {playing_file}", level=xbmc.LOGWARNING)
                    status.close()
                    notify("Resume: Kodi started a different video", xbmcgui.NOTIFICATION_WARNING)
                elif saved_file and not file_matches:
                    log(f"Resume skipped seek: expected file {saved_file} but Kodi opened "
                        f"{playing_file}", level=xbmc.LOGWARNING)
                    status.close()
                    notify("Resume: playback file mismatch", xbmcgui.NOTIFICATION_WARNING)
                else:
                    playback_verified[0] = True
                    offset = resume_point - int(Store.resume_offset)
                    try:
                        self.seekTime(offset if offset > 0 else resume_point)
                        log("%s restore: seek requested to %.1f seconds (saved %.1f seconds)" % (resume_label.capitalize(), offset if offset > 0 else resume_point, resume_point), level=xbmc.LOGINFO)
                    except RuntimeError:
                        log("Resume seek failed", level=xbmc.LOGWARNING)
            else:
                log("Playback was not ready for resume seek", level=xbmc.LOGWARNING)
        started, complete = video_queue.resume_id_first(
            queue, position=position, monitor=monitor, after_open=seek_saved_video,
            before_open=before_open, status=status)
        if not started:
            xbmc.executebuiltin("Dialog.Close(busydialognocancel,true)", False)
        if not started:
            log("Video resume could not start the saved item", level=xbmc.LOGWARNING)
            return False
        if not complete:
            log("Video resume first item playing, but queue restoration incomplete", level=xbmc.LOGWARNING)
            return True
        log(f"Restored saved video queue via ID-first XSP")
        self._clear_music_after_video_restore(monitor, verified=playback_verified[0])
        return True

    @timing.span("player.get_random_library_video")
    def get_random_library_video(self, first_only=False, tvshow_id=None, ignored_ids=None, media_kind=None):
        """Return eligible Kodi library IDs without recursive retries or modal dialogs.

        Try each enabled content type at most once. A show with no remaining
        eligible episodes is a valid result (the first episode can play alone).
        """
        if Store.kodi_event_monitor and Store.kodi_event_monitor.abortRequested():
            return []
        if ignored_ids is None:
            # Use the same short-lived smart-playlist exclusion cache as the
            # context menu. A single Auto Play Random run collects it once.
            status.update(0, "Auto Play Random", "Selecting videos...")
            from .context import collect_ignored_ids
            library_cache.ensure_fresh(Store.kodi_event_monitor)
            ignored_ids = set(collect_ignored_ids())

        enabled_types = []
        if get_setting_as_bool("randomtv") and Store.video_types_in_library.get("episodes"):
            enabled_types.append(("episodes", "GetEpisodes"))
        if get_setting_as_bool("randommovies") and Store.video_types_in_library.get("movies"):
            enabled_types.append(("movies", "GetMovies"))
        if get_setting_as_bool("randommusicvideos") and Store.video_types_in_library.get("musicvideos"):
            enabled_types.append(("musicvideos", "GetMusicVideos"))

        if media_kind is not None:
            enabled_types = [row for row in enabled_types if row[0] == media_kind + "s"]
        if tvshow_id is not None:
            if not any(kind == "episodes" for kind, _ in enabled_types):
                log("Auto Play Random: TV episode selection is disabled")
                return []
            # Constrain the rest of an episode queue to its original show.
            candidates = [("episodes", "GetEpisodes")]
        else:
            if not enabled_types:
                log("Auto Play Random: no enabled video types in library")
                return []
            start = randint(0, len(enabled_types) - 1)
            candidates = enabled_types[start:] + enabled_types[:start]

        target = max(1, int(Store.randomitems))
        for result_type, method in candidates:
            if Store.kodi_event_monitor and Store.kodi_event_monitor.abortRequested():
                return []
            # Fetch extra candidates when some might be excluded by smart
            # playlists. One first-only candidate can be excluded, so asking
            # Kodi for just one used to leave us with an empty result.
            limit_count = (max(50, target * 2)
                           if ignored_ids else (1 if first_only else target))
            kind = result_type[:-1]
            cached = library_cache.select(
                kind, 1 if first_only else target, ignored_ids=ignored_ids,
                tvshowid=tvshow_id, monitor=Store.kodi_event_monitor)
            if cached is not None:
                if cached:
                    log(f"Auto Play Random: selected {len(cached)} cached {result_type}")
                    return cached
                continue
            timing.mark("Auto Play Random library selection FALLBACK source=direct_RPC method=%s" % method)
            query_params = {
                "limits": {"end": limit_count},
                "sort": {"method": "random"}
            }
            if tvshow_id is not None:
                query_params["tvshowid"] = tvshow_id

            query = {
                "jsonrpc": "2.0", "id": "randomLibraryVideo",
                "method": "VideoLibrary." + method,
                "params": query_params
            }
            try:
                response = json.loads(timing.execute(json.dumps(query)))
            except (ValueError, TypeError) as error:
                log(f"Auto Play Random: bad {method} response: {error}")
                continue
            if "error" in response:
                log(f"Auto Play Random: {method} failed: {response['error']}")
                continue
            video_list = response.get("result", {}).get(result_type, [])
            item_key = result_type[:-1] + "id"
            item_type = result_type[:-1]
            eligible = []
            for item in video_list:
                video_id = item.get(item_key)
                if isinstance(video_id, int) and (item_type, video_id) not in ignored_ids:
                    eligible.append({item_key: video_id})
                    if len(eligible) >= (1 if first_only else target):
                        break

            if eligible:
                log(f"Auto Play Random: selected {len(eligible)} {result_type}")
                return eligible
            log(f"Auto Play Random: no eligible {result_type}")
            # Do not recurse with tvshow_id. If the show is completely excluded
            # retrying the identical query can loop forever.
        return []

    @timing.span("player.autoplay_random_if_enabled")
    @with_status
    def autoplay_random_if_enabled(self, queue_completion=False, generation=None, idle_started=None, *, status):
        """Start an eligible random queue, always releasing the Kodi busy dialog.

        As in v1.1.34, build the complete queue before Player.Open so resume
        snapshots can never save only the first video of an in-progress queue.
        """
        enabled = lambda: (Store.autoplay_queue_completion if queue_completion else Store.autoplay_random)
        canceled = lambda: (not enabled() or (queue_completion and generation != self._completion_generation))
        if canceled() or getattr(self, "_autoplay_busy", False):
            return
        if Store.kodi_event_monitor and Store.kodi_event_monitor.abortRequested():
            return
        if self.isPlaying() or Store.was_trailer or Store.was_audio:
            return
        self._autoplay_busy = True
        try:
            monitor = Store.kodi_event_monitor or xbmc.Monitor()
            idle_started = idle_started or (time.monotonic(), xbmc.getGlobalIdleTime())
            idle_aborted = False

            def idle_canceled():
                nonlocal idle_aborted
                # Kodi exposes integer seconds; allow one second of rounding.
                elapsed = time.monotonic() - idle_started[0]
                if not idle_aborted and xbmc.getGlobalIdleTime() + 1 < idle_started[1] + elapsed:
                    idle_aborted = True
                    log(('Queue completion' if queue_completion else 'Startup/wake') + ' autoplay canceled: user activity detected')
                    status.close()
                    notify('Auto-Play Random canceled...', xbmcgui.NOTIFICATION_INFO)
                return idle_aborted

            def before_open():
                return not (canceled() or monitor.abortRequested() or self.isPlaying() or idle_canceled())

            if queue_completion:
                delay = max(int(Store.queue_completion_delay), 0)
                # Allow Kodi to advance naturally before deciding the queue is idle.
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    if canceled() or self.isPlaying() or monitor.abortRequested() or idle_canceled():
                        return
                    remaining = max(0, deadline - time.monotonic())
                    if monitor.waitForAbort(min(0.5, remaining)):
                        return
                if canceled() or self.isPlaying() or monitor.abortRequested() or idle_canceled():
                    return
            if not before_open():
                return
            status.update(0, "Auto Play Random", "Preparing to play random videos...")
            if not wait_preparing(monitor, before_open):
                return
            # Never activate the non-cancelable spinner here. The previous
            # implementation opened it in the library query and could leave
            # it stuck open on empty results, exceptions or recursive retries.
            status.update(0, "Auto Play Random", "Selecting videos...")
            from .context import collect_ignored_ids
            library_cache.ensure_fresh(Store.kodi_event_monitor)
            ignored_ids = set(collect_ignored_ids())
            first_items = self.get_random_library_video(first_only=True, ignored_ids=ignored_ids)
            if not first_items:
                log('Auto Play Random: no eligible video found')
                status.close()
                notify('Auto Play Random: no eligible videos', xbmcgui.NOTIFICATION_WARNING)
                return
            first_item = first_items[0]
            if 'musicvideoid' in first_item:
                status.update(0, "Auto Play Random", "Preparing to play random music videos...")
            remaining_items = []
            if 'episodeid' in first_item:
                details_query = {
                    "jsonrpc": "2.0", "method": "VideoLibrary.GetEpisodeDetails",
                    "params": {"episodeid": first_item['episodeid'], "properties": ["tvshowid"]},
                    "id": "get_episode"
                }
                details = json.loads(timing.execute(json.dumps(details_query)))
                tvshow_id = details.get('result', {}).get('episodedetails', {}).get('tvshowid')
                if isinstance(tvshow_id, int):
                    remaining_items = self.get_random_library_video(
                        first_only=False, tvshow_id=tvshow_id, ignored_ids=ignored_ids)
                else:
                    log('Auto Play Random: episode has no TV show ID; playing it alone')
            else:
                remaining_items = self.get_random_library_video(
                    first_only=False, ignored_ids=ignored_ids, media_kind=next(iter(first_item))[:-2])

            # Maintain original random order, but avoid duplicate queue IDs.
            queued = []
            seen = set()
            for item in [first_item] + remaining_items:
                key = tuple(sorted(item.items()))
                if key not in seen:
                    queued.append(item)
                    seen.add(key)
            if not before_open():
                log('Auto Play Random canceled before changing the playlist')
                return

            queued = queued[:max(1, int(Store.randomitems))]
            started, complete = video_queue.start_queue(
                queued, monitor=monitor, source="auto-random", random_order=True,
                before_open=before_open, status=status)
            if not started and (idle_aborted or canceled() or monitor.abortRequested() or self.isPlaying()):
                return
            if not started:
                log('Auto Play Random: could not start first video')
                status.close()
                notify('Auto Play Random: could not start playback', xbmcgui.NOTIFICATION_WARNING)
                return
            if not complete:
                log('Auto Play Random: playback started, but remaining queue is incomplete')
                status.close()
                notify('Auto Play Random: incomplete queue', xbmcgui.NOTIFICATION_WARNING)
                return
            log(f'Auto Play Random: loaded {len(queued)} videos via native XSP')
        except Exception as error:
            log(f'Auto Play Random failed: {error}', error)
            status.close()
            notify('Auto Play Random failed; check Kodi log', xbmcgui.NOTIFICATION_WARNING)
        finally:
            # Clear even a busy dialog left by a previous version of this code.
            xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
            self._autoplay_busy = False
