"""Native XSP video queue construction with strict library ID verification."""
if __package__:
    from . import timing
else:
    import timing

import json
import os
import sys
import time
import uuid
from collections import Counter, defaultdict
from xml.etree import ElementTree as ET
from urllib.parse import unquote

import xbmc
import xbmcgui
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
    xbmc.log('###Kodi Playlist Resumer - ' + ('Playlist Resumer video XSP: ' + str(message)), xbmc.LOGWARNING)


def _random_log(message, level=xbmc.LOGINFO):
    xbmc.log('###Kodi Playlist Resumer - ' + message, level)


def _random_label(items, source):
    kind = next(iter(items[0]), '') if items else ''
    media = {'musicvideoid': 'music videos', 'movieid': 'movies', 'episodeid': 'TV episodes'}.get(kind, 'videos')
    return ('Auto Play Random' if source == 'auto-random' else 'Play Random') + ' (' + media + ')'


def _timed_call(name, fn, *args, **kwargs):
    start = time.monotonic()
    timing.mark("NATIVE START " + name)
    try:
        return fn(*args, **kwargs)
    finally:
        timing.mark("NATIVE END %s elapsed=%.3fs" % (name, time.monotonic() - start))


def _rpc(method, params):
    try:
        return json.loads(timing.execute(json.dumps({
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


@timing.span("video_queue._resolve_paths")
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
        timing.mark("Path cache %s requested=%d hits=%d misses=%d" % (kind, len(ids), len(ids)-len(missing), len(missing)))
        if not missing:
            continue
        timing.mark("Path cache FALLBACK kind=%s source=direct_RPC mode=%s missing=%d" % (kind, "details" if len(missing) <= 4 else "bulk", len(missing)))
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


@timing.span("video_queue._native_queue_items")
def _native_queue_items(diag=None):
    playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
    count = playlist.size()
    rows = []
    unreadable = []
    for index in range(count):
        try:
            tag = playlist[index].getVideoInfoTag()
            kind = tag.getMediaType()
            dbid = tag.getDbId()
            if kind not in LIST_METHODS or type(dbid) is not int or dbid <= 0:
                raise ValueError('Invalid library identity')
            rows.append({kind + "id": dbid})
        except (IndexError, RuntimeError, AttributeError, TypeError, ValueError) as error:
            # Keep actual native indices; never retry a missing item.
            rows.append({})
            unreadable.append(index)
            _log('Skipping unreadable native index %d: %s' % (index, error))
    final_count = playlist.size()
    rows = rows[:final_count]
    changed = count != final_count
    timing.mark('Native queue scan start=%d end=%d read=%d invalid=%d changed=%s' % (
        count, final_count, len(rows), len(unreadable), changed))
    if diag is not None:
        diag['native_size_changed'] = changed
        diag['native_unreadable_indices'] = unreadable
    return rows


def _live_queue_items(diag=None):
    try:
        return _native_queue_items(diag)
    except Exception as error:
        timing.mark("Native queue read FAILED (no RPC fallback) reason=" + str(error))
        return None


def _keys(rows):
    if rows is None:
        return None
    return [tuple(sorted((_identity(row) or {}).items())) for row in rows]


def _filename(path):
    return unquote(path).replace(chr(92), '/').rsplit('/', 1)[-1]


@timing.span("video_queue._choose_xsp_rules")
def _choose_xsp_rules(kind, unique, random_order):
    include = list(unique.values())
    def use_include(reason):
        timing.mark("XSP rules mode=include values=%d reason=%s" % (len(include), reason))
        return include, False
    if not random_order:
        return use_include('exact_restore')
    try:
        from .library_cache import read_selection_cache, cache_max_age
    except ImportError:
        from library_cache import read_selection_cache, cache_max_age
    data = read_selection_cache()
    age = time.time() - float(data.get('built_at', 0))
    rows = data.get(kind)
    if not rows or not 0 <= age < cache_max_age():
        return use_include('cache_not_fresh')
    universe = {}
    for row in rows:
        if type(row.get('id')) is not int or not isinstance(row.get('file'), str) or not row['file']:
            return use_include('incomplete_cache_paths')
        universe[((kind + 'id', row['id']),)] = row['file']
    if any(universe.get(key) != path for key, path in unique.items()):
        return use_include('selected_paths_not_in_cache')
    excluded = [path for key, path in universe.items() if key not in unique]
    if len(excluded) >= len(include):
        return use_include('inclusion_cheaper')
    selected_names = {_filename(path).casefold() for path in include}
    excluded_names = {_filename(path).casefold() for path in excluded}
    if '' in selected_names or '' in excluded_names or selected_names & excluded_names:
        return use_include('ambiguous_filename')
    # Kodi text equality uses SQL LIKE. Avoid inverse rules containing wildcard
    # or escape characters rather than accidentally excluding selected items.
    if any(any(c in _filename(path) for c in ('%', '_', chr(92))) for path in excluded):
        return use_include('excluded_filename_like_wildcard')
    timing.mark("XSP rules mode=exclude values=%d include_values=%d universe=%d" % (len(excluded), len(include), len(universe)))
    return excluded, True


@timing.span("video_queue._write_xsp")
def _write_xsp(kind, paths, target, random_order=False, inverse=False):
    root = ET.Element('smartplaylist', {'type': kind + 's'})
    ET.SubElement(root, 'name').text = 'KPR internal queue'
    ET.SubElement(root, 'match').text = 'all' if inverse else 'one'
    if inverse:
        # Separate negative rules joined by AND: never OR negative values.
        for path in paths:
            rule = ET.SubElement(root, 'rule', {'field': 'filename', 'operator': 'isnot'})
            ET.SubElement(rule, 'value').text = _filename(path)
    else:
        rule = ET.SubElement(root, 'rule', {'field': 'filename', 'operator': 'is'})
        for path in paths:
            ET.SubElement(rule, 'value').text = _filename(path)
    if random_order:
        ET.SubElement(root, 'order', {'direction': 'ascending'}).text = 'random'
    ET.ElementTree(root).write(target, encoding='utf-8', xml_declaration=True)


def _checked(method, params):
    result = _rpc(method, params)
    if not _success(result):
        raise RuntimeError('%s: %s' % (method, result))
    return result


@timing.span("video_queue.clear_playlist_first")
def clear_playlist_first(monitor, playlistid=1, stop_video=False):
    """Clear and verify the relevant playlist before loading the new queue."""
    if stop_video and xbmc.Player().isPlayingVideo():
        _checked('Player.Stop', {'playerid': 1})
        deadline = time.monotonic() + 5
        while xbmc.Player().isPlayingVideo():
            if monitor.abortRequested() or time.monotonic() >= deadline:
                raise RuntimeError('Existing video playback did not stop')
            monitor.waitForAbort(0.1)
    for attempt in range(3):
        timing.mark("Clear playlist %d attempt %d/3" % (playlistid, attempt+1))
        if monitor.abortRequested():
            raise RuntimeError('Playlist clear aborted')
        try:
            started = time.monotonic()
            playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO if playlistid == 1 else xbmc.PLAYLIST_MUSIC)
            _checked('Playlist.Clear', {'playlistid': playlistid})
            timing.mark('Targeted RPC clear playlist=%d elapsed=%.3fs' % (playlistid, time.monotonic() - started))
            deadline = time.monotonic() + 2
            while not monitor.abortRequested() and time.monotonic() < deadline:
                if playlist.size() == 0:
                    xbmc.log('###Kodi Playlist Resumer - ' + ('Playlist Resumer: playlist %d cleared and verified' % playlistid), xbmc.LOGDEBUG)
                    return
                monitor.waitForAbort(0.1)
        except RuntimeError:
            pass
    raise RuntimeError('Could not verify empty playlist %d' % playlistid)


@timing.span("video_queue._append_xsp")
def _append_xsp(kind, paths, token, index, random_order=False, before_add=None, inverse=False):
    target = os.path.join(PROFILE, 'kpr_internal_%s_%d.xsp' % (token, index))
    try:
        _write_xsp(kind, paths, target, random_order=random_order, inverse=inverse)
        if before_add is not None:
            before_add()
        # Kodi expands the smart playlist directory natively and retains tags.
        _checked('Playlist.Add', {'playlistid': 1, 'item': {'directory': target, 'media': 'video', 'recursive': False}})
    finally:
        try:
            os.remove(target)
        except OSError:
            pass


@timing.span("video_queue._skip_missing")
def _skip_missing(expected, monitor, diag):
    """Wait for asynchronous native population, then scan once."""
    playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
    started = time.monotonic()
    deadline = started + 5.0
    last_size = None
    stable_since = started
    polls = 0
    reason = 'timeout'
    timing.mark("START native queue readiness expected=%d" % len(expected))
    while True:
        if monitor.abortRequested():
            raise RuntimeError('Aborted')
        size = playlist.size()
        now = time.monotonic()
        polls += 1
        if size != last_size:
            stable_since = now
            last_size = size
            timing.mark("Native queue readiness size=%d expected=%d" % (size, len(expected)))
        if size >= len(expected) and size > 0:
            reason = 'expected_count'
            break
        if size > 0 and now - stable_since >= 0.150:
            reason = 'stable_nonzero'
            break
        if now >= deadline:
            break
        if monitor.waitForAbort(min(0.025, max(0.0, deadline - now))):
            raise RuntimeError('Aborted')
    elapsed = time.monotonic() - started
    timing.mark("END native queue readiness reason=%s size=%d expected=%d polls=%d elapsed=%.3fs" % (reason, size, len(expected), polls, elapsed))
    diag['native_readiness'] = {'reason': reason, 'size': size, 'polls': polls, 'elapsed_s': round(elapsed, 3)}
    actual = _keys(_live_queue_items(diag))
    if actual is None:
        raise RuntimeError('Native queue unavailable')
    wanted = Counter(expected)
    for key, count in (wanted - Counter(actual)).items():
        for _ in range(count):
            item = dict(key)
            _log('Skipping missing XSP library item: ' + str(item))
            diag.setdefault('skipped_items', []).append(item)
    return actual


def _snapshot_store():
    if __package__:
        from .store import Store
    else:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if root not in sys.path:
            sys.path.insert(0, root)
        from resources.lib.store import Store
    if not Store.state_file:
        Store.state_file = os.path.join(PROFILE, "resume data.json")
    return Store

@timing.span("video_queue.start_queue")
def start_queue(items, monitor=None, after_open=None, source="restore", random_order=False, restore_position=None, before_open=None, status=None):
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
    queue_changed = False
    active_identity = None
    active_position = None
    began = time.monotonic()
    random_label = _random_label(items, source) if random_order else None
    if random_label:
        _random_log('%s: loading %d items' % (random_label, len(items)))
    diag = {'expected_size': len(items), 'success': False, 'mode': 'xsp-directory-append', 'source': source, 'ordering': 'native-xsp-random' if random_order else 'saved-exact'}
    diag.update(playback_deferred=True, requested_position=position)
    @timing.span("video_queue.open_saved_position")
    def open_saved_position():
        nonlocal started, active_identity, active_position
        _checked('Player.Open', {'item': {'playlistid': 1, 'position': position}, 'options': {'shuffled': False, 'repeat': 'off'}})
        deadline = time.monotonic() + 15
        while not monitor.abortRequested() and time.monotonic() < deadline:
            if not xbmc.Player().isPlayingVideo():
                monitor.waitForAbort(0.1)
                continue
            active = _rpc('Player.GetItem', {'playerid': 1, 'properties': ['file']}).get('result', {}).get('item')
            props = _rpc('Player.GetProperties', {'playerid': 1, 'properties': ['position', 'playlistid']}).get('result', {})
            if (_identity(active) and xbmc.Player().isPlayingVideo()
                    and type(props.get('position')) is int and props['position'] >= 0
                    and props.get('playlistid') == 1):
                started = True
                active_position = props['position']
                active_identity = _identity(active)
                diag['playing_position'] = active_position
                diag['playing_item'] = active_identity
                break
            monitor.waitForAbort(0.1)
        else:
            raise RuntimeError('No library video started from the new queue')
        _timed_call('Dialog.Close', xbmc.executebuiltin, 'Dialog.Close(busydialognocancel)')
        if after_open is not None and active_identity == resume_target:
            _timed_call('resume.after_open', after_open)
    try:
        if before_open is not None and not before_open():
            raise RuntimeError("Queue canceled before clearing")
        if status is not None:
            status.update(0, "Auto Play Random", "Preparing %d random %s..." % (len(items), "music videos" if all("musicvideoid" in item for item in items) else "videos"))
        paths = _resolve_paths(items, monitor)
        if paths is None:
            raise RuntimeError('Could not resolve saved library filenames')
        original_position = position
        resume_target = _identity(items[position]) if deferred else None
        valid = [(i, item, path, key) for i, (item, path, key) in enumerate(zip(items, paths, expected)) if path]
        diag['skipped_items'] = [dict(key) for key, path in zip(expected, paths) if not path]
        if not valid:
            raise RuntimeError('No library items have resolvable paths')
        items = [row[1] for row in valid]
        paths = [row[2] for row in valid]
        expected = [row[3] for row in valid]
        # A native video XSP has exactly one media type. Never silently drop
        # mixed-media saved queues or use extra XSP batches.
        kinds = {next(iter(_identity(item)))[:-2] for item in items}
        if len(kinds) != 1:
            raise RuntimeError('Single XSP cannot restore mixed video types')
        kind = next(iter(kinds))
        unique = {}
        for key, path in zip(expected, paths):
            unique.setdefault(key, path)
        timing.mark("XSP plan items=%d unique=%d batches=1 kind=%s" % (len(items), len(unique), kind))
        def clear_before_add():
            nonlocal queue_changed
            if monitor.abortRequested() or (before_open is not None and not before_open()):
                raise RuntimeError('Queue canceled before clearing')
            timing.mark('XSP prepared; clearing immediately before Playlist.Add')
            queue_changed = True
            clear_playlist_first(monitor, stop_video=True)
        rule_paths, inverse = _choose_xsp_rules(kind, unique, random_order)
        diag.update(xsp_rule_mode='exclude' if inverse else 'include', xsp_rule_values=len(rule_paths))
        if status is not None:
            status.update(0, "Auto Play Random", "Adding %d %s..." % (len(items), "music videos" if kind == "musicvideo" else "videos"))
        _append_xsp(kind, rule_paths, token, 1, random_order=random_order,
                    before_add=clear_before_add, inverse=inverse)
        queued = _skip_missing(list(unique), monitor, diag)
        for key in set(unique) - set(queued):
            _log("Missing XSP item diagnostic: %s filename=%r" % (dict(key), unquote(unique[key]).replace(chr(92), "/").rsplit("/", 1)[-1]))
        if inverse:
            # A fresh cache is not a transaction with the live library. Never
            # play unexpected IDs from an inverse query (new rows, duplicates).
            if diag.get('native_size_changed') or not all(queued) or xbmc.PlayList(xbmc.PLAYLIST_VIDEO).size() != len(queued):
                raise RuntimeError('Inverse XSP membership could not be verified')
            seen = set()
            extras = []
            for i, key in enumerate(queued):
                if key not in unique or key in seen:
                    extras.append(i)
                seen.add(key)
            for i in reversed(extras):
                _checked('Playlist.Remove', {'playlistid': 1, 'position': i})
                queued.pop(i)
            if extras:
                timing.mark('Inverse XSP removed unexpected entries=%d' % len(extras))
                verified = _keys(_live_queue_items())
                if verified != queued:
                    raise RuntimeError('Inverse XSP cleanup could not be verified')
            diag['inverse_removed_extras'] = len(extras)
        # Restore requested ordering where native indices are reliable. If a
        # scan changes size, keep Kodi's real order instead of aborting playback.
        stable = (not diag.get('native_size_changed') and all(queued)
                  and xbmc.PlayList(xbmc.PLAYLIST_VIDEO).size() == len(queued))
        if not random_order and stable:
            present = set(queued)
            for key, count in Counter(expected).items():
                if key in present:
                    for _ in range(count - 1):
                        try:
                            _checked('Playlist.Insert', {'playlistid': 1, 'position': len(queued), 'item': dict(key)})
                            queued.append(key)
                        except RuntimeError as error:
                            _log('Skipping unavailable duplicate: %s' % error)
        counts = Counter(queued)
        surviving = []
        for row in valid:
            if counts[row[3]]:
                surviving.append(row)
                counts[row[3]] -= 1
        if not any(queued):
            raise RuntimeError('No playable library items could be queued')
        desired = [row[3] for row in surviving]
        target = (next((row[3] for row in surviving if row[0] >= original_position), surviving[-1][3])
                  if deferred and surviving else desired[0] if desired else next(key for key in queued if key))
        if deferred and dict(target) != resume_target:
            after_open = None
            diag['saved_item_skipped'] = True
        actual = list(queued)
        swaps = 0
        ordering_start = time.monotonic()
        timing.mark("START ordering items=%d random=%s" % (len(desired), random_order))
        if stable and Counter(actual) == Counter(desired):
            positions = defaultdict(set)
            for i, key in enumerate(actual):
                positions[key].add(i)
            for i in (range(1) if random_order else range(len(desired))):
                if monitor.abortRequested():
                    raise RuntimeError('Aborted')
                wanted = desired[i]
                if actual[i] == wanted:
                    continue
                j = next((j for j in positions[wanted] if j > i), None)
                if j is None:
                    stable = False
                    break
                try:
                    _checked('Playlist.Swap', {'playlistid': 1, 'position1': i, 'position2': j})
                except RuntimeError as error:
                    _log('Keeping native queue after ordering change: %s' % error)
                    stable = False
                    break
                old = actual[i]
                positions[old].remove(i)
                positions[old].add(j)
                positions[wanted].remove(j)
                positions[wanted].add(i)
                actual[i], actual[j] = actual[j], actual[i]
                swaps += 1
        else:
            stable = False
            _log('Keeping available native queue order; no XSP retries')
        timing.mark("END ordering swaps=%d elapsed=%.3fs" % (swaps, time.monotonic()-ordering_start))
        live_size = xbmc.PlayList(xbmc.PLAYLIST_VIDEO).size()
        if live_size <= 0:
            raise RuntimeError('No videos remain in native playlist')
        # Prefer the requested video, but allow Kodi to play an available entry.
        position = next((i for i, key in enumerate(actual[:live_size]) if key == target),
                        next((i for i, key in enumerate(actual[:live_size]) if key), 0))
        if deferred and stable and actual == desired:
            preferred = next((i for i, row in enumerate(surviving) if row[0] >= original_position), len(surviving)-1)
            if 0 <= preferred < live_size:
                position = preferred
        diag['adjusted_position'] = position
        diag['effective_size'] = live_size
        if before_open is not None and before_open() is False:
            _log('XSP playback canceled before Player.Open')
            return False, False
        timing.mark("Queue ready items=%d position=%d" % (live_size, position))
        open_saved_position()
        # Capture the actual queue after playback when the initial scan or
        # ordering was uncertain. This is snapshot capture, not an XSP retry.
        if not stable or not random_order or xbmc.PlayList(xbmc.PLAYLIST_VIDEO).size() != len(actual) or active_position >= len(actual) or actual[active_position] != tuple(sorted(active_identity.items())):
            final_order = _keys(_live_queue_items()) or []
        else:
            final_order = actual
        # Ignore entries without a usable library identity and translate the
        # real playback index to the saved filtered queue's index.
        indexed = [(i, key) for i, key in enumerate(final_order) if key]
        active_key = tuple(sorted(active_identity.items()))
        matches = [i for i, (native_index, key) in enumerate(indexed) if key == active_key]
        exact = next((i for i, (native_index, key) in enumerate(indexed)
                      if native_index == active_position and key == active_key), None)
        final_order = [key for _, key in indexed]
        position = exact if exact is not None else matches[0] if matches else None
        # Replace the old snapshot with actual playback, not the requested list.
        Store = _snapshot_store()
        playing_path = _timed_call("Player.getPlayingFile", xbmc.Player().getPlayingFile)
        seconds = int(_timed_call("Player.getTime", xbmc.Player().getTime))
        if position is not None and not _timed_call("Store.is_excluded", Store.is_excluded, playing_path):
            _timed_call("Store.update_state", Store.update_state, "video", {"items": [dict(key) for key in final_order],
                "position": position, "seconds": seconds,
                "resume_item": {"item": active_identity, "file": playing_path},
                "updated_at": time.time()}, last_media="video")
        else:
            _log('No usable actual playback snapshot; clearing previous video resume')
            Store.update_state('video', {})
        diag.update(success=True, queue_size=len(final_order), swaps=swaps,
                    first=dict(final_order[0]) if final_order else None,
                    order_verified=stable and not random_order and final_order == desired,
                    membership_verified=stable, first_verified=bool(final_order and final_order[0] == target))
        complete = True
        if random_label:
            _random_log('%s: queued %d/%d items in %.2f seconds; playback confirmed at position %d; membership %s' % (
                random_label, len(final_order), len(items), time.monotonic()-began,
                active_position+1, 'verified' if stable else 'not verified'))
            if not stable or len(final_order) != len(items) or position is None:
                _random_log('%s: queue or playback verification incomplete' % random_label, xbmc.LOGWARNING)
        return True, True
    except Exception as error:
        diag['error'] = str(error)
        _log('%s failed after %.2f seconds: %s' % (random_label or 'XSP queue', time.monotonic()-began, error))
        if queue_changed and not monitor.abortRequested():
            try:
                _snapshot_store().update_state('video', {})
                _log('Cleared previous video resume after queue failure')
            except Exception as snapshot_error:
                _log('Could not clear previous video resume: ' + str(snapshot_error))
        return started, False
    finally:
        diag['elapsed_s'] = round(time.monotonic() - began, 3)
        _remove_if_token(token)



@timing.span('video_queue.start_single_random')
def start_single_random(items, monitor, progress, source='random-library'):
    """Manual random: start one library item, then append remaining items via XSP."""
    token = uuid.uuid4().hex
    began = time.monotonic()
    started = complete = False
    random_label = _random_label(items, source)
    _random_log('%s: loading %d items' % (random_label, len(items)))
    target = None
    playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
    player = xbmc.Player()
    def guard():
        if monitor.abortRequested() or (_read_json(MARKER) or {}).get('token') != token:
            raise RuntimeError('Random queue canceled or superseded')
    timing.mark('Single random diagnostic: progress=on refresh=off first=library-id remainder=xsp foreground_snapshot=off')
    try:
        _atomic_json(MARKER, {'token':token,'started':time.time(),'status':'xsp-loading','expected_size':len(items)})
        paths = _resolve_paths(items, monitor)
        if paths is None:
            raise RuntimeError('Could not resolve library paths')
        unique = {}
        for key, path in zip(_keys(items), paths):
            if key and path:
                unique.setdefault(key, path)
        if not unique:
            raise RuntimeError('No playable items')
        kinds = {key[0][0][:-2] for key in unique}
        if len(kinds) != 1:
            raise RuntimeError('Mixed video types unsupported')
        kind = next(iter(kinds))
        preferred = next(iter(unique))
        remaining = {key: path for key, path in unique.items() if key != preferred}
        if remaining:
            rules, inverse = _choose_xsp_rules(kind, remaining, True)
            target = os.path.join(PROFILE, 'kpr_internal_%s_1.xsp' % token)
            _write_xsp(kind, rules, target, random_order=True, inverse=inverse)
            timing.mark('ID-first random prepared planned=%d remainder=%d rules=%d inverse=%s' % (len(unique),len(remaining),len(rules),inverse))
        guard()
        clear_playlist_first(monitor, stop_video=True)
        playlist.unshuffle()
        _snapshot_store().update_state('video', {})
        guard()
        # Add only the explicitly selected item with Kodi's library metadata.
        _checked('Playlist.Add', {'playlistid':1, 'item':dict(preferred)})
        timing.mark('ID-first selected add returned')
        # Wait only for the first item to exist, never for the remaining queue.
        deadline = time.monotonic() + 5.0
        while playlist.size() < 1:
            guard()
            if time.monotonic() >= deadline or monitor.waitForAbort(.025):
                raise RuntimeError('Selected library item not queued')
        guard()
        _checked('Player.Open', {'item':{'playlistid':1,'position':0},'options':{'shuffled':False,'repeat':'off'}})
        deadline = time.monotonic()+15
        while time.monotonic()<deadline:
            guard()
            if player.isPlayingVideo():
                active = _identity(_rpc('Player.GetItem', {'playerid':1,'properties':['file']}).get('result',{}).get('item'))
                if active == dict(preferred) and playlist.getposition()==0:
                    started = True
                    break
            monitor.waitForAbort(.1)
        if not started:
            raise RuntimeError('Playback not confirmed')
        for dialog in ('busydialognocancel', 'busydialog'):
            _timed_call('GUI.Dialog.Close.' + dialog, xbmc.executebuiltin, 'Dialog.Close(%s,true)' % dialog, False)
        timing.mark('ID-first playback confirmed elapsed=%.3fs' % (time.monotonic()-began))
        if target:
            guard()
            progress.update(max(1, 100 // len(unique)), 'Play Random', 'Adding remaining %s...' % ('music videos' if kind == 'musicvideo' else 'videos'))
            guard()
            _checked('Playlist.Add', {'playlistid':1, 'item':{'directory':target, 'media':'video', 'recursive':False}})
            timing.mark('ID-first remainder append returned elapsed=%.3fs' % (time.monotonic()-began))
        # Report observed count without rescanning or changing the queue.
        actual_size = playlist.size()
        _random_log('%s: queued %d/%d items observed in %.2f seconds; first item playback confirmed at position 1; append accepted' % (
            random_label, actual_size, len(items), time.monotonic()-began))
        if actual_size != len(items):
            _random_log('%s: queue count differs at append return; membership not verified' % random_label, xbmc.LOGWARNING)
        complete = True  # append accepted, not verified membership
        timing.mark('ID-first random completed; actual queue capture deferred to service')
        return started, complete
    except Exception as error:
        _log('%s interrupted after %.2f seconds: %s' % (random_label, time.monotonic()-began, error))
        return started, False
    finally:
        if target:
            try:
                os.remove(target)
            except OSError:
                pass
        _remove_if_token(token)
        timing.mark('ID-first random finished started=%s submitted=%s elapsed=%.3fs' % (started,complete,time.monotonic()-began))

@timing.span('video_queue.resume_id_first')
def resume_id_first(items, position, monitor, after_open, before_open, status=None):
    """Start saved ID immediately, then restore the original queue during playback."""
    expected = _keys(items)
    if not expected or not all(expected) or not 0 <= position < len(expected):
        return False, False
    preferred = expected[position]
    kinds = {key[0][0][:-2] for key in expected}
    if len(kinds) != 1:
        _log('Mixed video types unsupported for XSP resume')
        return False, False
    kind = next(iter(kinds))
    token = uuid.uuid4().hex
    playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
    player = xbmc.Player()
    started = complete = False
    began = time.monotonic()
    resume_label = "Music video" if kind == "musicvideo" else "Video"
    def guard():
        if monitor.abortRequested() or (_read_json(MARKER) or {}).get('token') != token:
            raise RuntimeError('Resume canceled or superseded')
        if started and not player.isPlayingVideo():
            raise RuntimeError('Resumed playback stopped')
    def verify_current():
        guard()
        active = _identity(_rpc('Player.GetItem', {'playerid':1,'properties':['file']}).get('result',{}).get('item'))
        if active != dict(preferred):
            raise RuntimeError('Resumed video changed during queue restoration')
    try:
        if not before_open():
            return False, False
        xbmc.log('###Kodi Playlist Resumer - Restoring %s playlist: %d items, position %d' % (resume_label.lower(), len(items), position + 1), xbmc.LOGINFO)
        _atomic_json(MARKER, {'token':token,'started':time.time(),'status':'xsp-loading','expected_size':len(items),'first':dict(preferred)})
        guard()
        clear_playlist_first(monitor)
        playlist.unshuffle()
        if not before_open():
            raise RuntimeError('Resume canceled before starting video')
        _checked('Playlist.Add', {'playlistid':1,'item':dict(preferred)})
        deadline = time.monotonic()+5
        while playlist.size() < 1:
            guard()
            if time.monotonic() >= deadline or monitor.waitForAbort(.025):
                raise RuntimeError('Saved item not queued')
        if not before_open():
            raise RuntimeError('Resume canceled before Player.Open')
        _checked('Player.Open', {'item':{'playlistid':1,'position':0},'options':{'shuffled':False,'repeat':'off'}})
        deadline = time.monotonic()+15
        while time.monotonic()<deadline:
            guard()
            if player.isPlayingVideo():
                active = _identity(_rpc('Player.GetItem', {'playerid':1,'properties':['file']}).get('result',{}).get('item'))
                if active == dict(preferred) and playlist.getposition()==0:
                    started = True
                    break
            monitor.waitForAbort(.1)
        if not started:
            raise RuntimeError('Saved video playback not confirmed')
        for dialog in ('busydialognocancel','busydialog'):
            xbmc.executebuiltin('Dialog.Close(%s,true)' % dialog, False)
        timing.mark('ID-first resume playback confirmed elapsed=%.3fs' % (time.monotonic()-began))
        if after_open:
            _timed_call('resume.after_open', after_open)
        verify_current()
        # Resolve only the remainder, after playback and seeking have started.
        remaining = list(dict.fromkeys(key for key in expected if key != preferred))
        if status is not None:
            status.update(0, "Resume Music Video" if kind == "musicvideo" else "Resume Video", "Adding %d %s..." % (len(remaining), "music videos" if kind == "musicvideo" else "videos"))
        unique = {}
        if remaining:
            paths = _resolve_paths([dict(key) for key in remaining], monitor)
            if paths is None:
                raise RuntimeError('Could not resolve remainder paths')
            unique = {key:path for key,path in zip(remaining,paths) if path}
            if unique:
                verify_current()
                rules, inverse = _choose_xsp_rules(kind, unique, False)
                _append_xsp(kind, rules, token, 1, random_order=False, inverse=inverse)
                timing.mark('ID-first resume remainder append returned')
                # Ordering requires a stable native queue; never delay initial playback.
                deadline = time.monotonic()+20
                last_size, stable_since = -1, time.monotonic()
                while True:
                    guard()
                    size = playlist.size()
                    now = time.monotonic()
                    if size != last_size:
                        last_size, stable_since = size, now
                    if size >= 1+len(unique) or (size > 1 and now-stable_since >= 2) or now >= deadline:
                        break
                    if monitor.waitForAbort(.05):
                        raise RuntimeError('Aborted during remainder loading')
                if size == 1:
                    raise RuntimeError('Remainder did not populate')
        verify_current()
        diag = {}
        actual = _keys(_live_queue_items(diag))
        if not actual or not all(actual) or diag.get('native_size_changed') or actual[0] != preferred:
            raise RuntimeError('Native resume queue not stable')
        # Remove accidental extra matches from filename rules, never the playing item.
        allowed = set(unique) | {preferred}
        seen = {preferred}
        extras = []
        for i,key in enumerate(actual[1:],1):
            if key not in allowed or key in seen:
                extras.append(i)
            seen.add(key)
        for i in reversed(extras):
            guard()
            _checked('Playlist.Remove', {'playlistid':1,'position':i})
            actual.pop(i)
        # Recreate saved duplicates by library ID; XSP only returns unique entries.
        present = set(actual)
        for key,count in Counter(expected).items():
            if key in present:
                for _ in range(count-1):
                    guard()
                    _checked('Playlist.Add', {'playlistid':1,'item':dict(key)})
                    actual.append(key)
        if _keys(_live_queue_items()) != actual:
            raise RuntimeError('Queue changed before restoring order')
        desired = [key for key in expected if key in present]
        # Reserve the original saved occurrence of the active item, including duplicates.
        active_target = sum(key in present for key in expected[:position])
        def swap(i,j):
            if i == j:
                return
            guard()
            if playlist.size() != len(actual):
                raise RuntimeError('Queue changed during ordering')
            _checked('Playlist.Swap', {'playlistid':1,'position1':i,'position2':j})
            actual[i],actual[j] = actual[j],actual[i]
        if status is not None:
            status.update(0, "Resume Music Video" if kind == "musicvideo" else "Resume Video", "Restoring playlist order...")
        swap(0,active_target)
        # Never move the active slot again; Kodi tracks it during the first swap.
        positions = defaultdict(set)
        for i,key in enumerate(actual):
            if i != active_target:
                positions[key].add(i)
        swaps = int(active_target != 0)
        for i,wanted in enumerate(desired):
            if i == active_target or actual[i] == wanted:
                if i != active_target:
                    positions[wanted].discard(i)
                continue
            j = next(iter(positions[wanted]), None)
            if j is None:
                raise RuntimeError('Cannot restore saved ordering')
            old = actual[i]
            swap(i,j)
            positions[old].discard(i)
            positions[old].add(j)
            positions[wanted].discard(j)
            swaps += 1
        verify_current()
        if playlist.getposition() != active_target or _keys(_live_queue_items()) != desired:
            raise RuntimeError('Restored order or active position could not be verified')
        complete = True
        xbmc.log('###Kodi Playlist Resumer - %s restore: queued %d/%d items in %.2f seconds; playlist and active item verified at position %d' % (resume_label, len(desired), len(expected), time.monotonic()-began, active_target+1), xbmc.LOGINFO)
        timing.mark('ID-first resume ordered size=%d requested=%d position=%d swaps=%d; snapshot deferred' % (len(desired),len(expected),active_target,swaps))
        return started, complete
    except Exception as error:
        _log('%s restore interrupted: %s (elapsed %.2f seconds)' % (resume_label, error, time.monotonic()-began))
        return started, False
    finally:
        _remove_if_token(token)
        timing.mark('ID-first resume finished started=%s complete=%s elapsed=%.3fs' % (started,complete,time.monotonic()-began))
