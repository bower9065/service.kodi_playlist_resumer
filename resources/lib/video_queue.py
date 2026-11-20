"""Video playlist fast-start with a cross-interpreter snapshot guard.

The context-menu script and the service run in different Python interpreters.
A profile marker prevents onAVStarted from overwriting the saved queue while
later items are still being appended.  Failure keeps the old snapshot intact.
"""
import json
import os
import time
import uuid

import xbmc
import xbmcvfs
from xbmcaddon import Addon

PROFILE = xbmcvfs.translatePath(Addon().getAddonInfo('profile'))
MARKER = os.path.join(PROFILE, 'video playlist loading.json')
MARKER_MAX_AGE = 300


def _log(message):
    xbmc.log('Playlist Resumer video queue: ' + str(message), xbmc.LOGINFO)


def _read_marker():
    try:
        with open(MARKER, 'r', encoding='utf-8') as f:
            marker = json.load(f)
        if not isinstance(marker, dict) or not marker.get('token'):
            return None
        if time.time() - float(marker.get('started', 0)) > MARKER_MAX_AGE:
            _log('expired stale incomplete queue marker')
            _remove_if_token(marker['token'])
            return None
        return marker
    except (OSError, ValueError, TypeError):
        return None


def _write_marker(marker):
    os.makedirs(PROFILE, exist_ok=True)
    tmp = MARKER + '.' + marker['token'] + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(marker, f)
    os.replace(tmp, MARKER)


def _remove_if_token(token):
    try:
        with open(MARKER, 'r', encoding='utf-8') as f:
            current = json.load(f)
        if current.get('token') == token:
            os.remove(MARKER)
    except (OSError, ValueError, TypeError):
        pass


def _identity(item):
    if not isinstance(item, dict):
        return None
    for kind in ('movie', 'episode', 'musicvideo'):
        value = item.get(kind + 'id')
        if isinstance(value, int):
            return {kind + 'id': value}
        if item.get('type') == kind and isinstance(item.get('id'), int):
            return {kind + 'id': item['id']}
    return None


def snapshots_blocked(current_item=None):
    """Keep old snapshot until ALL queued videos exist; unblock new media."""
    marker = _read_marker()
    if marker is None:
        return False
    # If the user chooses a different movie, this is no longer our queue.
    identity = _identity(current_item)
    if identity and identity != marker.get('first'):
        _log('another video started; releasing old queue guard')
        _remove_if_token(marker['token'])
        return False
    return True


def _rpc(method, params):
    response = xbmc.executeJSONRPC(json.dumps({
        'jsonrpc': '2.0', 'method': method, 'params': params, 'id': 'kpr_fast_video'
    }))
    try:
        return json.loads(response)
    except (TypeError, ValueError):
        return {'error': {'message': 'invalid JSON-RPC response'}}


def _success(result):
    return isinstance(result, dict) and 'result' in result and 'error' not in result


def start_queue(items, batch_size=200, monitor=None, after_open=None):
    """Return (started, complete). Start item 0 before appending queue tail.

    Any failed/incomplete queue retains the marker to suppress partial saves.
    A later unrelated playback or an expired marker automatically releases it.
    """
    if not items:
        return (False, False)
    # Context-menu scripts run in a separate interpreter and may not pass
    # the service's monitor. Give them their own shutdown signal.
    monitor = monitor if monitor is not None else xbmc.Monitor()
    if monitor.abortRequested():
        return (False, False)
    token = uuid.uuid4().hex
    first = _identity(items[0])
    marker = {'token': token, 'started': time.time(), 'first': first,
              'expected_size': len(items), 'status': 'loading'}
    _write_marker(marker)
    started = False
    try:
        if monitor.abortRequested():
            return (False, False)
        if not _success(_rpc('Playlist.Clear', {'playlistid': 1})):
            _log('could not clear playlist')
            return (False, False)
        if monitor.abortRequested():
            return (False, False)
        if not _success(_rpc('Playlist.Add', {'playlistid': 1, 'item': items[0]})):
            _log('could not add first video')
            return (False, False)
        if monitor.abortRequested():
            return (False, False)
        result = _rpc('Player.Open', {'item': {'playlistid': 1, 'position': 0},
                                     'options': {'shuffled': False}})
        if not _success(result):
            _log('could not open selected video: ' + str(result))
            return (False, False)
        started = True
        _log('opened first video; appending %d more' % (len(items) - 1))
        # The context menu and startup resume may have displayed Kodi's busy
        # overlay while selecting the first video. Close it here, immediately
        # after Player.Open succeeds, BEFORE the rest of the playlist is added.
        # The snapshot guard remains active until the queue is complete.
        try:
            xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
        except Exception as error:
            _log('could not close busy dialog after first video: %s' % error)
        if after_open is not None:
            try:
                after_open()
            except Exception as error:
                _log('post-open callback failed: %s' % error)
        for start in range(1, len(items), max(1, int(batch_size))):
            if monitor.abortRequested():
                _log('queue construction aborted')
                return (True, False)
            current = _read_marker()
            if not current or current.get('token') != token:
                _log('queue superseded by another playback')
                return (True, False)
            batch = items[start:start + max(1, int(batch_size))]
            result = _rpc('Playlist.Add', {'playlistid': 1, 'item': batch})
            if not _success(result):
                _log('batch failed at %d, retrying individual entries' % start)
                for item in batch:
                    if monitor.abortRequested():
                        _log('individual queue retry canceled for shutdown')
                        return (True, False)
                    if not _success(_rpc('Playlist.Add', {'playlistid': 1, 'item': item})):
                        _log('failed appending %s; preserving previous saved queue' % str(item))
                        return (True, False)
        if monitor.abortRequested():
            return (True, False)
        props = _rpc('Playlist.GetProperties', {'playlistid': 1, 'properties': ['size']})
        actual = props.get('result', {}).get('size')
        if actual != len(items):
            _log('incomplete live queue %s/%s; preserving previous snapshot' %
                 (actual, len(items)))
            return (True, False)
        # If another video replaces the first during construction, don't save
        # this queue.  Player.Open can return before the player is activated,
        # so a not-yet-active player is not itself an error.
        current_marker = _read_marker()
        if current_marker is None or current_marker.get('token') != token:
            _log('queue superseded just before completion')
            return (True, False)
        active = _rpc('Player.GetActivePlayers', {})
        videos_active = any(x.get('playerid') == 1 for x in
                            active.get('result', []) if isinstance(x, dict))
        if videos_active:
            current = _rpc('Player.GetItem', {'playerid': 1, 'properties': ['file']})
            playing = current.get('result', {}).get('item')
            playing_id = _identity(playing)
            if first and playing_id and playing_id != first:
                _log('active video changed during queue loading: %s' % playing_id)
                return (True, False)
        if monitor.abortRequested():
            return (True, False)
        _remove_if_token(token)
        _log('complete %d-item queue ready; resume snapshot unlocked' % len(items))
        return (True, True)
    except Exception as exc:
        _log('error while constructing queue: %s' % exc)
        return (started, False)
    finally:
        # Before Player.Open there is no partial playback to protect.
        # After Player.Open, keep the guard if any append/verification failed.
        if not started or monitor.abortRequested():
            # On a real Kodi shutdown callbacks are already abort-guarded.
            # Do not leave a stale marker suppressing snapshots next startup.
            _remove_if_token(token)
