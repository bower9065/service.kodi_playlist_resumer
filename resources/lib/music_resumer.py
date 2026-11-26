# -*- coding: utf-8 -*-
"""Persist and restore Kodi's audio playlist (playlist 0) independently of video.

The snapshot is written atomically only when the active song agrees with the
playlist.  Never overwrite a good snapshot with a partially loaded queue.
"""
if __package__:
    from . import timing
else:
    import timing

import json
import os
import time

import xbmc
from .common import log, notify, xbmcgui, wait_preparing
from .store import Store
from .status import with_status
from . import video_queue


def rpc(method, params):
    request = {'jsonrpc': '2.0', 'method': method, 'params': params,
               'id': 'playlist_resumer_music'}
    return json.loads(timing.execute(json.dumps(request)))


def checked_rpc(method, params):
    result = rpc(method, params)
    if not isinstance(result, dict) or 'error' in result or 'result' not in result:
        raise ValueError('%s failed: %s' % (method, result))
    return result['result']


def _as_queue_item(item):
    # Kodi returns 'id' (not 'songid') in the playlist response for library songs.
    if item.get('type') == 'song' and type(item.get('id')) is int and item['id'] > 0:
        return {'songid': item['id']}
    if isinstance(item.get('file'), str) and item['file']:
        return {'file': item['file']}
    return None


def _is_valid_entry(item):
    return isinstance(item, dict) and (
        (type(item.get('songid')) is int and item['songid'] > 0)
        or (isinstance(item.get('file'), str) and bool(item['file']))
    )


def _same_item(saved_item, active_item, saved_file, active_file):
    """Compare actual song identity before comparing file paths.

    Kodi sometimes reports a translated or differently encoded path for the
    same library song. Matching valid song IDs prevents a false 'wrong song'
    warning. File-based music (which lacks a library ID) still uses its path.
    """
    saved_item = saved_item or {}
    active_item = active_item or {}
    saved_id = saved_item.get('songid')
    active_id = (active_item.get('id') if active_item.get('type') == 'song'
                 else None)
    if (type(saved_id) is int and saved_id > 0 and
            type(active_id) is int and active_id > 0 and saved_id == active_id):
        return True

    # Use the JSON-RPC file as a fallback, too: getPlayingFile() may return a
    # different Kodi URL even when Player.GetItem.file is the original path.
    expected_paths = [saved_file, saved_item.get('file')]
    actual_paths = [active_file, active_item.get('file')]
    return any(a == b for a in expected_paths for b in actual_paths
               if isinstance(a, str) and a and isinstance(b, str) and b)


def _get_playing_item(player):
    """Read the actual playing song (not just the playlist's current index)."""
    if not player.isPlayingAudio():
        return None, None
    current = checked_rpc('Player.GetItem',
                          {'playerid': 0, 'properties': ['file']}).get('item', {})
    return current, player.getPlayingFile()


@timing.span("music_resumer._wait_for_saved_song")
def _wait_for_saved_song(player, saved_item, saved_file, monitor, seconds=4):
    """Allow Kodi's audio player to update after Player.Open/Player.GoTo."""
    deadline = time.monotonic() + seconds
    actual_item, actual_file = None, None
    while True:
        if monitor.abortRequested() or not player.isPlayingAudio():
            return False, actual_item, actual_file
        actual_item, actual_file = _get_playing_item(player)
        if _same_item(saved_item, actual_item, saved_file, actual_file):
            return True, actual_item, actual_file
        remaining = deadline - time.monotonic()
        if remaining <= 0 or monitor.waitForAbort(min(0.2, remaining)):
            return False, actual_item, actual_file



@timing.span("music_resumer._paths_for_queue")
def _paths_for_queue(snapshot, queue, monitor=None):
    """Resolve a saved queue to file URLs in one bulk library pass.

    New snapshots already contain URLs. Older snapshots contain only song IDs;
    resolving them via one AudioLibrary.GetSongs listing avoids the N-per-song
    database opens in Kodi's JSON-RPC Playlist.Add handler.
    """
    paths = snapshot.get('paths')
    if (isinstance(paths, list) and len(paths) == len(queue) and
            all(isinstance(p, str) and p and '\n' not in p and '\r' not in p
                for p in paths)):
        return paths
    paths = [item.get('file', '') for item in queue]
    missing = {item['songid'] for item, path in zip(queue, paths)
               if not path and type(item.get('songid')) is int}
    if missing:
        log('Music restore: resolving %d library song IDs in bulk' % len(missing),
            level=xbmc.LOGINFO)
        found = {}
        start = 0
        while missing:
            if monitor is not None and monitor.abortRequested():
                return None
            response = checked_rpc('AudioLibrary.GetSongs', {
                'properties': ['file'], 'includesingles': True,
                'limits': {'start': start, 'end': start + 2000}})
            songs = response.get('songs', [])
            if not isinstance(songs, list):
                raise ValueError('AudioLibrary.GetSongs did not return songs')
            for song in songs:
                songid, file_path = song.get('songid'), song.get('file')
                if songid in missing and isinstance(file_path, str) and file_path:
                    found[songid] = file_path
            start += len(songs)
            total = response.get('limits', {}).get('total', start)
            if not songs or start >= total:
                break
        paths = [path or found.get(item.get('songid'), '')
                 for item, path in zip(queue, paths)]
    if not all(isinstance(path, str) and path and
               '\n' not in path and '\r' not in path for path in paths):
        log('Music restore: missing song paths; using legacy restore fallback',
            level=xbmc.LOGINFO)
        return None
    return paths


@timing.span("music_resumer._load_native_queue")
def _load_native_queue(paths):
    """Use Kodi's in-process playlist loader instead of per-song DB lookups."""
    playlist_file = os.path.join(
        os.path.dirname(Store.state_file), 'music-resume.m3u8')
    temporary = playlist_file + '.tmp'
    with open(temporary, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write('#EXTM3U\n')
        for path in paths:
            handle.write(path + '\n')
    os.replace(temporary, playlist_file)
    try:
        playlist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
        playlist.load(playlist_file)
        size = playlist.size()
        if size != len(paths):
            raise ValueError('native load size mismatch (%s/%s)' %
                             (size, len(paths)))
        return size
    finally:
        # This is only a loader input, not another persistent resume snapshot.
        try:
            os.remove(playlist_file)
        except OSError:
            pass


def clear():
    Store.clear_music_snapshot()
    log('Music resume snapshot cleared')


def should_preserve_on_track_end():
    """An ended song is not the end of the playlist if another can play.

    Kodi fires onPlayBackEnded at track boundaries. Keep the snapshot until
    the next onAVStarted captures the new song; clear it only when the list
    has actually reached its end (and repeat isn't active).
    """
    snapshot = Store.get_music_snapshot()
    if not isinstance(snapshot, dict):
        return False
    items = snapshot.get('items')
    position = snapshot.get('position')
    if isinstance(items, list) and type(position) is int and 0 <= position < len(items) - 1:
        return True
    try:
        props = checked_rpc('Player.GetProperties',
                            {'playerid': 0, 'properties': ['repeat']})
        return props.get('repeat') in ('all', 'one')
    except (ValueError, TypeError, RuntimeError):
        return False


def has_snapshot():
    snapshot = Store.get_music_snapshot()
    return isinstance(snapshot, dict) and bool(snapshot)


def capture(player, periodic=False):
    """Save full audio playlist, exact current song and index."""
    if (getattr(player, '_restoring_music', False) or not player.isPlayingAudio()
            or (Store.kodi_event_monitor and Store.kodi_event_monitor.abortRequested())):
        return False
    try:
        active_file = player.getPlayingFile()
        if not active_file or Store.is_excluded(active_file):
            return False
        props = checked_rpc('Player.GetProperties',
                            {'playerid': 0, 'properties': ['position', 'playlistid']})
        if props.get('playlistid') not in (None, 0):
            return False
        signature = (active_file, props.get('position'), xbmc.PlayList(xbmc.PLAYLIST_MUSIC).size())
        generation = getattr(player, '_music_queue_generation', 0)
        if (periodic and getattr(player, '_music_capture_signature', None) == (signature, generation)
                and time.monotonic() - getattr(player, '_music_full_capture', 0) < 60):
            return True
        playlist_result = checked_rpc('Playlist.GetItems',
                                      {'playlistid': 0, 'properties': ['file']})
        items = playlist_result.get('items', [])
        # GetItems can be paginated. Never mistake its first page for the
        # complete queue; request all entries if Kodi has limited the result.
        total = playlist_result.get('limits', {}).get('total', len(items))
        if isinstance(items, list) and type(total) is int and total > len(items):
            playlist_result = checked_rpc('Playlist.GetItems',
                {'playlistid': 0, 'properties': ['file'],
                 'limits': {'start': 0, 'end': total}})
            items = playlist_result.get('items', [])
            if len(items) != total:
                log('Music capture skipped: playlist result incomplete')
                return False
        current = checked_rpc('Player.GetItem',
                              {'playerid': 0, 'properties': ['file']}).get('item', {})
        if not isinstance(items, list):
            return False
        if items:
            # Do not silently drop unavailable items or destroy a saved queue.
            queue = [_as_queue_item(item) for item in items]
            if any(item is None for item in queue):
                log('Music capture skipped: unrecognized item in audio queue')
                return False
            position = props.get('position')
            if type(position) is not int or not 0 <= position < len(queue):
                position = -1
            matches = [i for i, item in enumerate(items)
                       if _same_item(queue[i], current, item.get('file'), active_file)]
            if not matches:
                log('Music capture skipped: active song not present in loaded queue')
                return False
            position = position if position in matches else matches[0]
        else:
            # Direct playback that does not populate the audio playlist.
            queue = [_as_queue_item(current) or {'file': active_file}]
            position = 0

        seconds = 0
        snapshot = {'version': 2, 'items': queue, 'position': position,
                    'item': queue[position], 'file': active_file, 'seconds': seconds,
                    'paths': ([item.get('file') for item in items] if items
                              else [active_file])}
        snapshot['updated_at'] = time.time()
        Store.save_music_snapshot(snapshot)
        player._music_capture_signature = (signature, generation)
        player._music_full_capture = time.monotonic()
        return True
    except (ValueError, TypeError, RuntimeError, OSError, KeyError) as error:
        log('Music capture skipped: %s' % error)
        return False


@timing.span("music_resumer.restore")
@with_status
def restore(player, before_open=lambda: True, *, status):
    """Restore a full music queue and start the saved song from the beginning."""
    if not Store.resume_on_startup or not has_snapshot() or player.isPlaying():
        return False
    try:
        snapshot = Store.get_music_snapshot()
        if not isinstance(snapshot, dict):
            return False
        queue = snapshot['items']
        position = snapshot['position']
        seconds = float(snapshot['seconds'])
        saved_item = snapshot['item']
        saved_file = snapshot.get('file')
        if (not isinstance(queue, list) or not queue or
            any(not _is_valid_entry(item) for item in queue) or
            type(position) is not int or not 0 <= position < len(queue) or
            queue[position] != saved_item or seconds < 0):
            log('Music restore: invalid snapshot; leaving it intact')
            return False
    except (OSError, ValueError, TypeError, KeyError) as error:
        log('Music restore: unreadable snapshot: %s' % error)
        return False

    monitor = Store.kodi_event_monitor or xbmc.Monitor()
    if monitor.abortRequested() or player.isPlaying():
        return False
    log('Restoring music playlist: %d tracks, position %d' % (len(queue), position + 1),
        level=xbmc.LOGINFO)
    if not before_open():
        return False
    status.update(0, "Resume Music", "Preparing to resume music playlist...")
    if not wait_preparing(monitor, before_open):
        return False
    queue_started = time.monotonic()
    player._restoring_music = True
    try:
        status.update(0, "Resume Music", "Restoring %d tracks..." % len(queue))
        video_queue.clear_playlist_first(monitor, playlistid=0)
        # Native loader processes a local M3U in Kodi's playlist engine.
        # JSON-RPC Playlist.Add with songid iterates a database GetSong per ID.
        paths = None
        try:

            paths = _paths_for_queue(snapshot, queue, monitor=monitor)
            if monitor.abortRequested():
                return False
            if paths is not None:
                if monitor.abortRequested():
                    return False

                _load_native_queue(paths)
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            log('Music native loader failed: %s; using legacy fallback' % error,
                level=xbmc.LOGINFO)
            paths = None
        if monitor.abortRequested():
            return False
        if paths is None:
            # Preserve compatibility with older snapshots and non-library URLs.
            # This is slow because Kodi resolves each song ID in its DB.

            checked_rpc('Playlist.Clear', {'playlistid': 0})
            for start in range(0, len(queue), 200):
                if monitor.abortRequested():
                    return False
                chunk = queue[start:start + 200]

                try:
                    checked_rpc('Playlist.Add', {'playlistid': 0, 'item': chunk})
                except (ValueError, TypeError) as error:
                    log('Music batch add failed: %s; retrying individually' % error,
                        level=xbmc.LOGINFO)
                    for item in chunk:
                        if monitor.abortRequested():
                            return False
                        checked_rpc('Playlist.Add', {'playlistid': 0, 'item': item})
                if monitor.abortRequested():
                    return False
        if monitor.abortRequested():
            return False

        props = checked_rpc('Playlist.GetProperties',
                            {'playlistid': 0, 'properties': ['size']})
        if props.get('size') != len(queue):
            log('Music restore: incomplete queue (%s/%s)' %
                (props.get('size'), len(queue)), level=xbmc.LOGWARNING)
            return False
        log('Music restore: queued %d tracks in %.2f seconds' %
            (len(queue), time.monotonic() - queue_started), level=xbmc.LOGINFO)
        # Kodi must not re-shuffle the saved order before selecting position.
        if monitor.abortRequested():
            return False
        if not before_open():
            return False
        status.update(0, "Resume Music", "Starting saved track...")

        checked_rpc('Player.Open', {'item': {'playlistid': 0, 'position': position},
                                    'options': {'shuffled': False}})

        # Wait up to 10 seconds for a real audio track, not just Player.Open OK.
        for _ in range(50):
            if player.isPlayingAudio():
                break
            if monitor.waitForAbort(0.2):
                return False
        if not player.isPlayingAudio():
            log('Music restore: player did not start audio', level=xbmc.LOGINFO)
            return False

        matched, playing_item, active_file = _wait_for_saved_song(
            player, saved_item, saved_file, monitor)
        if not matched and player.isPlayingAudio() and not monitor.abortRequested():
            # Kodi can open a saved music queue at the wrong index (or report
            # its previous track until the player catches up). Retry just once
            # at the saved index; verify the correct song before completing restoration.
            log('Music restore: expected song not active; retrying saved index %s' %
                position, level=xbmc.LOGINFO)
            try:
                checked_rpc('Player.GoTo', {'playerid': 0, 'to': position})
                matched, playing_item, active_file = _wait_for_saved_song(
                    player, saved_item, saved_file, monitor)
            except (ValueError, TypeError, RuntimeError) as error:
                log('Music restore: corrected-position retry failed: %s' % error,
                    level=xbmc.LOGINFO)
        if not matched:
            if not player.isPlayingAudio() or monitor.abortRequested():
                log('Music restore: playback interrupted during verification',
                    level=xbmc.LOGINFO)
                return False
            log('Music restore: wrong song opened (expected %s, playing %s)' %
                (saved_file or saved_item, active_file or playing_item),
                level=xbmc.LOGINFO)
            player._preserve_music_on_stop = True
            player.stop()
            return False
        if monitor.abortRequested():
            return False
        log('Music restore: starting saved song from beginning (no resume seek)', level=xbmc.LOGINFO)
        log('Music restore: playlist and track verified at position %d' % (position + 1), level=xbmc.LOGINFO)
        return True
    except (ValueError, TypeError, RuntimeError, OSError, KeyError) as error:
        log('Music restore failed: %s' % error, level=xbmc.LOGWARNING)
        return False
    finally:
        player._restoring_music = False
