"""On-demand configurable library cache with incremental event updates."""
if __package__:
    from . import timing
else:
    import timing

import json
import os
import random
import threading
import time

import xbmc
import xbmcvfs
from xbmcaddon import Addon

CACHE_NAME = "play_random_library_cache.json"
CACHE_VERSION = 2  # Version 2 stores file paths for native XSP queues.
IGNORED_NAME = "play_random_ignored_cache.json"
# Use smaller reads to let playback queue loading interrupt between JSON-RPC
# requests. The cache runs in the background, so throughput is not critical.
PAGE_SIZE = 250
VIDEO_QUEUE_MARKER = "video playlist loading.json"
KINDS = ("movie", "episode", "musicvideo")
METHODS = {"movie": "GetMovies", "episode": "GetEpisodes", "musicvideo": "GetMusicVideos"}
RESULTS = {"movie": "movies", "episode": "episodes", "musicvideo": "musicvideos"}
DETAIL_METHODS = {"movie": "GetMovieDetails", "episode": "GetEpisodeDetails",
                  "musicvideo": "GetMusicVideoDetails"}
# The context-menu script and the service are independent Python interpreters.
_lock = threading.RLock()


def _log(message, level=xbmc.LOGINFO):
    xbmc.log('###Kodi Playlist Resumer - ' + ("Play Random library cache: " + str(message)), level)


def _paths():
    profile = xbmcvfs.translatePath(Addon().getAddonInfo("profile"))
    return os.path.join(profile, CACHE_NAME), os.path.join(profile, IGNORED_NAME)


def _marker():
    return _paths()[0] + ".rebuild"


def _empty():
    return {"version": CACHE_VERSION, "movie": None, "episode": None, "musicvideo": None}


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and data.get("version") == CACHE_VERSION:
            for kind in KINDS:
                if kind not in data or (data[kind] is not None and not isinstance(data[kind], list)):
                    return _empty()
            return data
    except (OSError, ValueError, TypeError):
        pass
    return _empty()


_read_cache_lock = threading.RLock()
_read_cache_entry = None


@timing.span("library_cache.read_selection_cache")
def read_selection_cache():
    """Reuse parsed selection data until the atomic cache file is replaced."""
    global _read_cache_entry
    path = _paths()[0]
    with _read_cache_lock:
        try:
            stat = os.stat(path)
            key = (path, stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except OSError as error:
            timing.mark("Library cache MISS source=disk reason=%s" % type(error).__name__)
            _read_cache_entry = None
            return _empty()
        if _read_cache_entry is None or _read_cache_entry[0] != key:
            _read_cache_entry = (key, _read(path))
            source = "disk"
        else:
            source = "memory"
        data = _read_cache_entry[1]
        timing.mark("Library cache snapshot source=%s sections=%s" % (source, {k: len(data[k]) if data[k] is not None else "missing" for k in KINDS}))
        return data


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.isfile(temporary):
            os.remove(temporary)


class CacheLock:
    """Cross-process file lock, interruptible for Kodi shutdown."""
    def __init__(self, path, monitor=None, timeout=3):
        self.directory = path + ".lock"
        self.monitor = monitor
        self.timeout = timeout
        self.acquired = False

    def __enter__(self):
        end = time.monotonic() + self.timeout
        while time.monotonic() < end:
            if self.monitor is not None and self.monitor.abortRequested():
                break
            try:
                os.makedirs(os.path.dirname(self.directory), exist_ok=True)
                os.mkdir(self.directory)
                self.acquired = True
                break
            except FileExistsError:
                try:
                    if time.time() - os.stat(self.directory).st_mtime > 120:
                        os.rmdir(self.directory)
                        continue
                except OSError:
                    pass
                if self.monitor is not None:
                    if self.monitor.waitForAbort(0.05):
                        break
                else:
                    time.sleep(0.05)
            except OSError:
                break
        return self.acquired

    def __exit__(self, exc_type, exc, tb):
        if self.acquired:
            try:
                os.rmdir(self.directory)
            except OSError:
                pass


def _playlist_is_loading():
    """True while this add-on is constructing/restoring a video playlist.

    The context-menu script runs in a separate interpreter, so this must use
    the already-existing marker file rather than a thread-only flag.
    """
    path = os.path.join(os.path.dirname(_paths()[0]), VIDEO_QUEUE_MARKER)
    try:
        return 0 <= time.time() - os.path.getmtime(path) < 300
    except OSError:
        return False


@timing.span("library_cache._yield_to_playback")
def _yield_to_playback(monitor=None):
    """Pause cache work while a video queue is being populated."""
    while _playlist_is_loading():
        if monitor is not None:
            if monitor.waitForAbort(0.25):
                return False
        else:
            time.sleep(0.25)
    return monitor is None or not monitor.abortRequested()


def _rpc(method, params, monitor=None):
    if monitor is not None and monitor.abortRequested():
        return None
    try:
        request = json.dumps({"jsonrpc": "2.0", "id": "playRandomCache",
                              "method": method, "params": params})
        response = json.loads(timing.execute(request))
        if isinstance(response, dict) and "error" not in response:
            return response.get("result")
        _log("JSON-RPC failed for " + method + ": " + str(response), xbmc.LOGWARNING)
    except (ValueError, TypeError, RuntimeError) as error:
        _log("JSON-RPC error for " + method + ": " + str(error), xbmc.LOGWARNING)
    return None


@timing.span("library_cache._collect_kind")
def _collect_kind(kind, monitor=None):
    """Fetch IDs and playable file paths for native video M3U playlists."""
    collected = {}
    start = 0
    while True:
        if not _yield_to_playback(monitor):
            return None
        # Yield between pages so Kodi can service playback and UI requests.
        if start and monitor is not None and monitor.waitForAbort(0.15):
            return None
        params = {"limits": {"start": start, "end": start + PAGE_SIZE},
                  "properties": (["file", "tvshowid", "season"] if kind == "episode"
                                 else ["file"])}
        result = _rpc("VideoLibrary." + METHODS[kind], params, monitor)
        if not isinstance(result, dict) or not isinstance(result.get("limits"), dict):
            return None
        rows = result.get(RESULTS[kind], [])
        if not isinstance(rows, list):
            return None
        for item in rows:
            if not isinstance(item, dict):
                continue
            item_id = item.get(kind + "id")
            if type(item_id) is not int or item_id < 1:
                continue
            row = {"id": item_id, "file": item.get("file") or ""}
            if kind == "episode":
                row.update(tvshowid=item.get("tvshowid"), season=item.get("season"))
            collected[item_id] = row
        start = int(result["limits"].get("end", start + len(rows)))
        total = int(result["limits"].get("total", start))
        if start >= total or not rows:
            break
    return list(collected.values())


def invalidate_smart_cache(reason="unspecified"):
    _log("Smart cache INVALIDATE reason=" + str(reason))
    # A persistent operational marker survives Kodi restarts. Its generation
    # is compared with the generation captured before a smart cache rebuild.
    path = _paths()[1] + ".invalidated"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            os.utime(path, None)
    except OSError as error:
        _log("Smart cache invalidation failed: " + str(error), xbmc.LOGWARNING)


def smart_generation():
    try:
        return os.stat(_paths()[1] + ".invalidated").st_mtime_ns
    except OSError:
        return 0


def cache_max_age():
    """Read the shared cache lifetime in hours at request time."""
    try:
        hours = Addon().getSettingInt("cache_lifetime_hours")
        if 1 <= hours <= 24:
            return hours * 3600
    except Exception:
        pass
    return 24 * 3600


@timing.span("library_cache.ensure_fresh")
def ensure_fresh(monitor=None):
    """Refresh only when random playback requests a missing or expired cache."""
    path = _paths()[0]
    def fresh(data):
        missing = [k for k in KINDS if data.get(k) is None]
        age = time.time() - float(data.get("built_at", 0))
        limit = cache_max_age()
        valid = not missing and 0 <= age < limit
        reason = "fresh" if valid else ("missing_sections=" + ",".join(missing) if missing else ("no_build_timestamp" if not data.get("built_at") else "expired_or_future_timestamp"))
        timing.mark("Library cache freshness %s reason=%s age_hours=%s limit_hours=%.2f" % ("HIT" if valid else "MISS", reason, "unknown" if not data.get("built_at") else "%.3f" % (age/3600), limit/3600))
        return valid
    if fresh(read_selection_cache()):
        return True
    with CacheLock(path, monitor=monitor, timeout=10) as acquired:
        if not acquired:
            timing.mark("Library cache rebuild skipped reason=lock_unavailable")
            return False
        timing.mark("Library cache freshness recheck source=disk")
        if fresh(_read(path)):
            return True
        timing.mark("Library cache REBUILD START reason=missing_or_expired")
        data = _empty()
        started = time.time()
        for kind in KINDS:
            rows = _collect_kind(kind, monitor)
            if rows is None:
                _log("On-demand rebuild failed; previous cache retained", xbmc.LOGWARNING)
                return False
            data[kind] = rows
        if monitor is not None and monitor.abortRequested():
            return False
        data["built_at"] = time.time()
        _write(path, data)
        timing.mark("Library cache REBUILD END elapsed=%.3fs" % (time.time()-started))
        _log("On-demand library rebuild complete: " + str({k: len(data[k]) for k in KINDS}))
        return True


@timing.span("library_cache.select")
def select(kind, count, ignored_ids=(), tvshowid=None, season=None, monitor=None):
    """Read a ready index only; never build one in Play Random's script."""
    if kind not in KINDS or (monitor is not None and monitor.abortRequested()):
        return None
    data = read_selection_cache()
    rows = data[kind]
    if rows is None:
        timing.mark("Library selection cache MISS kind=%s reason=section_missing" % kind)
        return None
    timing.mark("Library selection cache HIT kind=%s entries=%d" % (kind, len(rows)))
    ignored = set(ignored_ids)
    eligible = []
    for row in rows:
        video_id = row.get("id")
        if type(video_id) is not int or (kind, video_id) in ignored:
            continue
        if tvshowid is not None and row.get("tvshowid") != tvshowid:
            continue
        if season is not None and row.get("season") != season:
            continue
        eligible.append({kind + "id": video_id})
    selected = random.sample(eligible, min(max(int(count), 0), len(eligible)))
    timing.mark("Library selection cache kind=%s eligible=%d selected=%d" % (kind, len(eligible), len(selected)))
    return selected


def _get_updated_item(kind, item_id, monitor=None):
    """Read only an affected item; RPC failure does not imply deletion."""
    params = {kind + "id": item_id, "properties": (["file", "tvshowid", "season"]
                                                   if kind == "episode" else ["file"])}
    result = _rpc("VideoLibrary." + DETAIL_METHODS[kind], params, monitor)
    if result is None:
        return False, None
    details = result.get(kind + "details") if isinstance(result, dict) else None
    if not isinstance(details, dict):
        return True, None
    row = {"id": item_id, "file": details.get("file") or ""}
    if kind == "episode":
        row.update(tvshowid=details.get("tvshowid"), season=details.get("season"))
    return True, row


def apply_updates(changes, monitor=None):
    """Update file paths and episode metadata only for changed library items."""
    if not changes or (monitor is not None and monitor.abortRequested()):
        return False
    path, _ = _paths()
    # Let a requested clear/rebuild win over any older queued notifications.
    if not os.path.isfile(path) or os.path.isfile(_marker()):
        return False
    # Collapse multiple notifications for the same item before any I/O.
    changes_by_id = {(kind, item_id): action for action, kind, item_id in changes
                     if type(item_id) is int and item_id > 0}
    if not changes_by_id:
        return False
    # Read outside lock: Play Random can continue using the current snapshot.
    if not _yield_to_playback(monitor):
        return False
    initial = _read(path)
    lookups = {}
    for (kind, item_id), action in changes_by_id.items():
        if not _yield_to_playback(monitor):
            return False
        if kind not in KINDS or initial.get(kind) is None or action == "remove":
            continue
        successful, item = _get_updated_item(kind, item_id, monitor)
        if successful:
            lookups[(kind, item_id)] = item
    if not _yield_to_playback(monitor):
        return False
    with _lock:
        with CacheLock(path, monitor=monitor, timeout=5) as acquired:
            if not acquired or os.path.isfile(_marker()):
                return False
            data = _read(path)
            changed = False
            for (kind, item_id), action in changes_by_id.items():
                if monitor is not None and monitor.abortRequested():
                    return False
                if kind == "tvshow" and action == "remove" and data["episode"] is not None:
                    old_len = len(data["episode"])
                    data["episode"] = [row for row in data["episode"]
                                       if row.get("tvshowid") != item_id]
                    changed |= len(data["episode"]) != old_len
                    continue
                if kind not in KINDS or data[kind] is None:
                    continue
                rows_by_id = {row["id"]: row for row in data[kind]
                              if isinstance(row, dict) and type(row.get("id")) is int}
                old = rows_by_id.get(item_id)
                if action == "remove":
                    new = None
                else:
                    if (kind, item_id) not in lookups:
                        data["built_at"] = 0
                        changed = True
                        continue  # Lookup failure: do not delete or add anything.
                    new = lookups[(kind, item_id)]
                if old != new:
                    rows_by_id.pop(item_id, None)
                    if new is not None:
                        rows_by_id[item_id] = new
                    data[kind] = list(rows_by_id.values())
                    changed = True
            if changed:
                _write(path, data)
                _log("Applied {} affected library notifications".format(len(changes_by_id)))
            return True


def request_library_rebuild(monitor=None):
    """Ask the service to rebuild without deleting the current usable index."""
    path, _ = _paths()
    with CacheLock(path, monitor=monitor, timeout=5) as acquired:
        if not acquired:
            return False
        try:
            with open(_marker(), "w", encoding="utf-8") as marker:
                marker.write("rebuild\n")
            return True
        except OSError as error:
            _log("Rebuild request failed: " + str(error), xbmc.LOGERROR)
            return False


class BackgroundCacheWorker:
    """One service-owned daemon thread; no JSON-RPC work in UI callbacks."""
    def __init__(self, monitor):
        self.monitor = monitor
        self._cv = threading.Condition()
        self._updates = {}
        self._last_change = 0
        self._scanning = False
        self._rebuild = False
        self._thread = threading.Thread(target=self._run, name="PlayRandomCache", daemon=True)

    def start(self):
        self._thread.start()

    def wake_if_cleared(self):
        """Called by the existing service tick; never starts a timed refresh."""
        if os.path.isfile(_marker()):
            with self._cv:
                self._rebuild = True
                self._cv.notify_all()

    def on_notification(self, method, raw_data):
        if method == "VideoLibrary.OnScanStarted":
            with self._cv:
                self._scanning = True
            return
        if method in ("VideoLibrary.OnScanFinished", "VideoLibrary.OnCleanFinished"):
            with self._cv:
                self._scanning = False
                self._cv.notify_all()
            return
        if method not in ("VideoLibrary.OnUpdate", "VideoLibrary.OnRemove"):
            return
        try:
            payload = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
            item = payload.get("item", payload)
            kind, item_id = item.get("type"), item.get("id")
            if kind not in KINDS + ("tvshow",) or type(item_id) is not int or item_id < 1:
                return
            action = "remove" if method == "VideoLibrary.OnRemove" else "update"
            flags = dict(item)
            flags.update(payload)
        except (ValueError, TypeError, AttributeError):
            return
        with self._cv:
            scanning = self._scanning
        reason = None
        if action == "remove":
            reason = "item_removed"
        elif flags.get("added") is True:
            reason = "item_added"
        elif scanning or flags.get("transaction") is True:
            reason = "scan_or_transaction_update"
        else:
            # Kodi's generic OnUpdate does not identify which metadata changed.
            # Static exclusions can wait for TTL/manual rebuild; dynamic rules
            # retain conservative invalidation on ambiguous playback updates.
            try:
                with open(_paths()[1], "r", encoding="utf-8") as f:
                    smart = json.load(f)
                sensitive = smart.get("playback_sensitive", True) is not False
            except (OSError, ValueError, TypeError, AttributeError):
                sensitive = True
            if sensitive:
                reason = "playback_sensitive_or_unknown_rules"
            elif flags.get("playcount", -1) != -1:
                reason = None
        details = "%s %s:%s flags=%r" % (method, kind, item_id, {k: flags[k] for k in ("added", "playcount", "transaction") if k in flags})
        if reason:
            invalidate_smart_cache(reason + " " + details)
        else:
            _log("Smart cache KEEP reason=static_rules_nonstructural_update " + details)
        # Keep incremental library index maintenance unchanged.
        with self._cv:
            self._updates[(kind, item_id)] = action
            self._last_change = time.monotonic()
            self._cv.notify_all()

    @timing.span("library_cache._build_missing")
    def _build_missing(self):
        path, _ = _paths()
        for kind in KINDS:
            if self.monitor.abortRequested():
                return
            with self._cv:
                if self._scanning:
                    return
            if os.path.isfile(_marker()):
                return
            if _read(path)[kind] is not None:
                continue
            _log("Building {} cache in background".format(kind))
            rows = _collect_kind(kind, self.monitor)
            if rows is None:
                _log("Could not build {} cache; original Play Random queries remain available".format(kind),
                     xbmc.LOGWARNING)
                continue
            with _lock:
                with CacheLock(path, monitor=self.monitor, timeout=10) as acquired:
                    if not acquired or self.monitor.abortRequested() or os.path.isfile(_marker()):
                        return
                    data = _read(path)
                    if data[kind] is None:
                        data[kind] = rows
                        _write(path, data)
                        _log("Background index ready: {} {} entries".format(len(rows), kind))

    @timing.span("library_cache._rebuild_all")
    def _rebuild_all(self):
        path, _ = _paths()
        data = _empty()
        for kind in KINDS:
            with self._cv:
                if self._scanning:
                    request_library_rebuild(self.monitor)
                    return
            rows = _collect_kind(kind, self.monitor)
            if rows is None:
                _log("Manual rebuild failed; previous cache retained", xbmc.LOGWARNING)
                return
            data[kind] = rows
        with CacheLock(path, monitor=self.monitor, timeout=10) as acquired:
            if acquired and not self.monitor.abortRequested():
                data["built_at"] = time.time()
                _write(path, data)
                _log("Manual library rebuild complete: " + str({k: len(data[k]) for k in KINDS}))
                import xbmcgui
                xbmcgui.Dialog().notification("Playlist Resumer", "Library cache rebuilt")

    def _run(self):
        # Give the Kodi UI and startup playback restoration an initial head start.
        if self.monitor.waitForAbort(5):
            return
        while not self.monitor.abortRequested():
            # A settings action runs in a separate interpreter. Watch its tiny
            # marker directly, even while the main service is restoring media.
            if os.path.isfile(_marker()):
                with self._cv:
                    self._rebuild = True
            with self._cv:
                scanning = self._scanning
                rebuilding = self._rebuild
            if scanning or _playlist_is_loading():
                with self._cv:
                    self._cv.wait(timeout=0.5)
                continue
            if rebuilding:
                with self._cv:
                    self._rebuild = False
                marker = _marker()
                manual = os.path.isfile(marker)
                if manual:
                    # A clear request supersedes any queued events.
                    with self._cv:
                        self._updates.clear()
                    try:
                        os.remove(marker)
                    except OSError:
                        pass
                try:
                    if manual:
                        self._rebuild_all()
                    else:
                        self._build_missing()
                except (OSError, ValueError, TypeError, RuntimeError) as error:
                    _log("Background build error: " + str(error), xbmc.LOGERROR)
                continue
            with self._cv:
                if not self._updates:
                    self._cv.wait(timeout=0.5)
                    continue
                # Batch scan and playback notifications into one file write.
                if time.monotonic() - self._last_change < 1.5:
                    self._cv.wait(timeout=0.25)
                    continue
                changes = [(action, kind, item_id) for (kind, item_id), action
                           in self._updates.items()]
                self._updates.clear()
            if os.path.isfile(_marker()):
                with self._cv:
                    self._rebuild = True
            else:
                try:
                    apply_updates(changes, self.monitor)
                except (OSError, ValueError, TypeError, RuntimeError) as error:
                    _log("Incremental update error: " + str(error), xbmc.LOGERROR)
