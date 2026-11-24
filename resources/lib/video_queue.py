"""Native XSP video queue construction with strict library ID verification."""
import json
import os
import sys
import time
import uuid
from collections import Counter, defaultdict
from xml.etree import ElementTree as ET
from urllib.parse import unquote

import xbmc
import xbmcvfs
from xbmcaddon import Addon

PROFILE = xbmcvfs.translatePath(Addon().getAddonInfo('profile'))
MARKER = os.path.join(PROFILE, 'video playlist loading.json')
MARKER_MAX_AGE = 300
ID_KEYS = {'movieid': ('movie', 'GetMovieDetails', 'moviedetails'),
           'episodeid': ('episode', 'GetEpisodeDetails', 'episodedetails'),
           'musicvideoid': ('musicvideo', 'GetMusicVideoDetails', 'musicvideodetails')}
LIST_METHODS = {'movie': ('GetMovies', 'movies'),
                'episode': ('GetEpisodes', 'episodes'),
                'musicvideo': ('GetMusicVideos', 'musicvideos')}


def _log(message):
    xbmc.log('Playlist Resumer video XSP: ' + str(message), xbmc.LOGINFO)


def _rpc(method, params):
    try:
        return json.loads(xbmc.executeJSONRPC(json.dumps({
            'jsonrpc': '2.0', 'method': method, 'params': params, 'id': 'kpr_xsp'})))
    except (TypeError, ValueError):
        return {'error': {'message': 'invalid JSON-RPC response'}}


def _success(result):
    return isinstance(result, dict) and 'result' in result and 'error' not in result


def _atomic_json(path, value):
    os.makedirs(PROFILE, exist_ok=True)
    tmp = path + '.' + uuid.uuid4().hex + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.isfile(tmp):
            os.remove(tmp)


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, TypeError, ValueError):
        return None


def _remove_if_token(token):
    marker = _read_json(MARKER)
    if isinstance(marker, dict) and marker.get('token') == token:
        try:
            os.remove(MARKER)
        except OSError:
            pass


def _read_marker():
    marker = _read_json(MARKER)
    if not isinstance(marker, dict) or not marker.get('token'):
        return None
    if time.time() - float(marker.get('started', 0)) > MARKER_MAX_AGE:
        _log('expired stale XSP loading guard')
        _remove_if_token(marker['token'])
        return None
    return marker


def _identity(item):
    if not isinstance(item, dict):
        return None
    for key, (kind, _, _) in ID_KEYS.items():
        value = item.get(key)
        if type(value) is int:
            return {key: value}
        if item.get('type') == kind and type(item.get('id')) is int:
            return {key: item['id']}
    return None


def snapshots_blocked(current_item=None):
    marker = _read_marker()
    if marker is None:
        return False
    return True


def _resolve_paths(items, monitor):
    """Use the background library cache; fetch uncached paths if necessary.

    On older installations the v1 ID-only cache is rebuilt in the background.
    Meanwhile fetch *only missing* file data, using a one-item details request
    for a handful of IDs or a paginated bulk library query for many IDs.
    """
    try:
        from .library_cache import read_selection_cache
    except ImportError:
        from library_cache import read_selection_cache
    data = read_selection_cache()
    lookups = {}
    for kind in LIST_METHODS:
        for row in data.get(kind) or []:
            if isinstance(row, dict) and type(row.get('id')) is int and row.get('file'):
                lookups[(kind, row['id'])] = row['file']
    required = {}
    for item in items:
        identity = _identity(item)
        if not identity:
            _log('unsupported XSP library item: ' + str(item))
            return None
        key, video_id = next(iter(identity.items()))
        kind = ID_KEYS[key][0]
        required.setdefault(kind, set()).add(video_id)
    for kind, ids in required.items():
        missing = {video_id for video_id in ids if (kind, video_id) not in lookups}
        if not missing:
            continue
        if len(missing) <= 4:
            for video_id in missing:
                if monitor.abortRequested():
                    return None
                key = kind + 'id'
                detail = _rpc('VideoLibrary.' + ID_KEYS[key][1],
                              {key: video_id, 'properties': ['file']})
                result = detail.get('result', {}).get(ID_KEYS[key][2], {})
                if result.get('file'):
                    lookups[(kind, video_id)] = result['file']
        else:
            method, result_key = LIST_METHODS[kind]
            offset = 0
            while missing:
                if monitor.abortRequested():
                    return None
                result = _rpc('VideoLibrary.' + method,
                              {'properties': ['file'], 'limits': {'start': offset, 'end': offset + 500}})
                result = result.get('result', {})
                rows = result.get(result_key, [])
                if not isinstance(rows, list):
                    return None
                for row in rows:
                    video_id = row.get(kind + 'id')
                    if video_id in missing and row.get('file'):
                        lookups[(kind, video_id)] = row['file']
                        missing.remove(video_id)
                limits = result.get('limits', {})
                new_offset = limits.get('end', offset + len(rows))
                if not rows or new_offset <= offset or new_offset >= limits.get('total', new_offset):
                    break
                offset = new_offset
    paths = []
    for item in items:
        identity = _identity(item)
        key, video_id = next(iter(identity.items()))
        filepath = lookups.get((ID_KEYS[key][0], video_id))
        if (not isinstance(filepath, str) or not filepath or
                '\r' in filepath or '\n' in filepath):
            _log('missing/unsafe file path for ' + str(item))
            filepath = None
        paths.append(filepath)
    return paths


def _live_queue_items(properties=None):
    rows = []
    total = None
    while True:
        start = len(rows)
        result = _rpc('Playlist.GetItems', {'playlistid': 1,
            'properties': properties if properties is not None else [],
            'limits': {'start': start, 'end': start + 500}})
        if not _success(result):
            return None
        data = result.get('result', {})
        page = data.get('items', [])
        limits = data.get('limits', {})
        count = limits.get('total')
        if not isinstance(page, list) or not isinstance(count, int):
            return None
        if total is not None and total != count:
            return None
        total = count
        if limits.get('start') != start or limits.get('end') != start + len(page):
            return None
        rows.extend(page)
        if len(rows) == total:
            return rows
        if not page or len(rows) > total:
            return None


def _keys(rows):
    if rows is None:
        return None
    return [tuple(sorted((_identity(row) or {}).items())) for row in rows]


def _write_xsp(kind, paths, target, random_order=False):
    root = ET.Element('smartplaylist', {'type': kind + 's'})
    ET.SubElement(root, 'name').text = 'KPR internal queue'
    ET.SubElement(root, 'match').text = 'one'
    rule = ET.SubElement(root, 'rule', {'field': 'filename', 'operator': 'is'})
    for path in paths:
        ET.SubElement(rule, 'value').text = unquote(path).replace('\\', '/').rsplit('/', 1)[-1]
    if random_order:
        ET.SubElement(root, 'order', {'direction': 'ascending'}).text = 'random'
    # Do not limit the query: ambiguous filenames must be detected, not truncated.
    ET.ElementTree(root).write(target, encoding='utf-8', xml_declaration=True)


def _checked(method, params):
    result = _rpc(method, params)
    if not _success(result):
        raise RuntimeError('%s: %s' % (method, result))
    return result


def _wait_queue(expected, monitor, ordered=False, timeout=15):
    deadline = time.monotonic() + timeout
    wanted = Counter(expected)
    while not monitor.abortRequested() and time.monotonic() < deadline:
        rows = _live_queue_items()
        keys = _keys(rows)
        if keys is not None and (keys == expected if ordered else Counter(keys) == wanted):
            return keys
        if monitor.waitForAbort(0.1):
            break
    actual = _keys(_live_queue_items())
    raise RuntimeError('Queue verification failed: expected %d library items; got %s; missing=%s; extra=%s' % (len(expected), len(actual) if actual is not None else 'unreadable', list((wanted - Counter(actual or [])).items())[:5], list((Counter(actual or []) - wanted).items())[:5]))


def _append_xsp(kind, paths, token, index, random_order=False):
    target = os.path.join(PROFILE, 'kpr_internal_%s_%d.xsp' % (token, index))
    _write_xsp(kind, paths, target, random_order=random_order)
    try:
        # Kodi expands the smart playlist directory natively and retains tags.
        _checked('Playlist.Add', {'playlistid': 1, 'item': {'directory': target, 'media': 'video', 'recursive': False}})
    finally:
        try:
            os.remove(target)
        except OSError:
            pass


def _skip_missing(expected, monitor, diag):
    """Log missing entries and keep the native XSP results; do not retry."""
    if monitor.abortRequested():
        raise RuntimeError('Aborted')
    wanted = Counter(expected)
    actual = _keys(_live_queue_items())
    if actual is None:
        raise RuntimeError('Queue unreadable during XSP verification')
    seen = Counter()
    extra_positions = []
    for pos, key in enumerate(actual):
        seen[key] += 1
        if seen[key] > wanted[key]:
            extra_positions.append(pos)
    for pos in reversed(extra_positions):
        _checked('Playlist.Remove', {'playlistid': 1, 'position': pos})
    if extra_positions:
        diag['removed_extras'] = diag.get('removed_extras', 0) + len(extra_positions)
        actual = _keys(_live_queue_items())
        if actual is None or Counter(actual) - wanted:
            raise RuntimeError('Queue verification failed after removing extras')
    for key, count in (wanted - Counter(actual)).items():
        for _ in range(count):
            item = dict(key)
            _log('Skipping missing XSP library item: ' + str(item))
            diag.setdefault('skipped_items', []).append(item)
    return actual


def start_queue(items, batch_size=500, monitor=None, after_open=None, source="restore", random_order=False, restore_position=None, before_open=None):
    monitor = monitor or xbmc.Monitor()
    if not items or monitor.abortRequested():
        return False, False
    expected = _keys(items)
    if not all(expected):
        return False, False
    deferred = restore_position is not None
    position = restore_position if deferred else 0
    if not 0 <= position < len(items) or (deferred and random_order):
        return False, False
    token = uuid.uuid4().hex
    _atomic_json(MARKER, {'token': token, 'started': time.time(), 'first': items[position], 'expected_size': len(items), 'status': 'xsp-loading'})
    started = False
    complete = False
    began = time.monotonic()
    diag = {'expected_size': len(items), 'success': False, 'mode': 'xsp-directory-append', 'source': source, 'ordering': 'native-xsp-random' if random_order else 'saved-exact'}
    diag.update(playback_deferred=True, requested_position=position)
    def open_saved_position():
        nonlocal started
        _checked('Player.Open', {'item': {'playlistid': 1, 'position': position}, 'options': {'shuffled': False, 'repeat': 'off'}})
        deadline = time.monotonic() + 15
        while not monitor.abortRequested() and time.monotonic() < deadline:
            active = _rpc('Player.GetItem', {'playerid': 1, 'properties': ['file']}).get('result', {}).get('item')
            props = _rpc('Player.GetProperties', {'playerid': 1, 'properties': ['position', 'playlistid']}).get('result', {})
            if (_identity(active) == _identity(items[position]) and xbmc.Player().isPlayingVideo()
                    and (not deferred or (props.get('position') == position and props.get('playlistid') == 1))):
                started = True
                diag['playing_position'] = props.get('position')
                diag['playing_item'] = _identity(active)
                break
            monitor.waitForAbort(0.1)
        else:
            raise RuntimeError('Correct library item did not start at requested position')
        xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
        if after_open is not None:
            after_open()
    try:
        paths = _resolve_paths(items, monitor)
        if paths is None:
            raise RuntimeError('Could not resolve saved library filenames')
        original_position = position
        valid = [(i, item, path, key) for i, (item, path, key) in enumerate(zip(items, paths, expected)) if path]
        diag['skipped_items'] = [dict(key) for key, path in zip(expected, paths) if not path]
        if not valid:
            raise RuntimeError('No library items have resolvable paths')
        items = [row[1] for row in valid]
        paths = [row[2] for row in valid]
        expected = [row[3] for row in valid]
        _checked('Playlist.Clear', {'playlistid': 1})
        _wait_queue([], monitor, ordered=True)
        # Include the selected item in the ordinary XSP batches; no separate
        # first-item query. Repeated IDs must be in distinct batches.
        groups = {}
        for item, path, key in zip(items, paths, expected):
            kind = next(iter(_identity(item)))[:-2]
            batches = groups.setdefault(kind, [])
            if not batches or len(batches[-1][0]) >= batch_size or key in batches[-1][1]:
                batches.append(([], set()))
            batches[-1][0].append((key, path))
            batches[-1][1].add(key)
        index = 0
        for kind, batches in groups.items():
            for batch, _ in batches:
                if monitor.abortRequested():
                    raise RuntimeError('Aborted')
                index += 1
                # An RPC failure is not evidence of individual missing files.
                _append_xsp(kind, [x[1] for x in batch], token, index, random_order=random_order)
        # Playlist.Add is synchronous. Read membership once after all adds;
        # log and skip absent IDs, with no missing-item polling or repair.
        queued = _skip_missing(expected, monitor, diag)
        counts = Counter(queued)
        surviving = []
        for row in valid:
            if counts[row[3]]:
                surviving.append(row)
                counts[row[3]] -= 1
        if not surviving:
            raise RuntimeError('No playable library items could be queued')
        items = [row[1] for row in surviving]
        expected = [row[3] for row in surviving]
        position = next((i for i, row in enumerate(surviving) if row[0] >= original_position), len(surviving)-1) if deferred else 0
        if deferred and surviving[position][0] != original_position:
            after_open = None
            diag['saved_item_skipped'] = True
        diag['adjusted_position'] = position
        diag['effective_size'] = len(expected)
        actual = queued
        swaps = 0
        # Index positions once, including repeated IDs, and update on swaps.
        positions = defaultdict(set)
        for i, key in enumerate(actual):
            positions[key].add(i)
        targets = range(len(expected)) if not random_order else range(1)
        for i in targets:
            if monitor.abortRequested():
                raise RuntimeError('Aborted')
            wanted = expected[i]
            if actual[i] == wanted:
                continue
            j = next((j for j in positions[wanted] if j > i), None)
            if j is None:
                raise RuntimeError('Expected library item missing during ordering')
            _checked('Playlist.Swap', {'playlistid': 1, 'position1': i, 'position2': j})
            old = actual[i]
            positions[old].remove(i)
            positions[old].add(j)
            positions[wanted].remove(j)
            positions[wanted].add(i)
            actual[i], actual[j] = actual[j], actual[i]
            swaps += 1
        # Only reread before playback if swaps actually changed the queue.
        final_order = _wait_queue(actual, monitor, ordered=True) if swaps else actual
        if not deferred and final_order[0] != expected[0]:
            raise RuntimeError('First queue entry changed')
        if before_open is not None and before_open() is False:
            _log('XSP playback canceled before Player.Open')
            return False, False
        open_saved_position()
        # Confirm the exact order survived Player.Open and use it for snapshot.
        final_order = _wait_queue(final_order, monitor, ordered=True)
        if final_order[position] != expected[position]:
            raise RuntimeError("Playing item position changed after opening queue")
        # Commit the verified queue and playback time together, before releasing the guard.
        if __package__:
            from .store import Store
        else:
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            if root not in sys.path:
                sys.path.insert(0, root)
            from resources.lib.store import Store
        if not Store.state_file:
            Store.state_file = os.path.join(PROFILE, "resume data.json")
        playing_path = xbmc.Player().getPlayingFile()
        seconds = int(xbmc.Player().getTime())
        if not Store.is_excluded(playing_path):
            Store.update_state("video", {"items": [dict(key) for key in final_order],
                "position": position, "seconds": seconds,
                "resume_item": {"item": items[position], "file": playing_path},
                "updated_at": time.time()}, last_media="video")
        diag.update(success=True, queue_size=len(final_order), swaps=swaps,
                    first=items[0], order_verified=True if not random_order else None,
                    membership_verified=True, first_verified=True)
        complete = True
        return True, True
    except Exception as error:
        diag['error'] = str(error)
        _log('XSP queue failed: ' + str(error))
        return started, False
    finally:
        diag['elapsed_s'] = round(time.monotonic() - began, 3)
        _log(str(diag))
        _remove_if_token(token)
