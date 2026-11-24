import sys
import xbmc
import xbmcgui
import json
import os
import time
import xbmcvfs
from xbmcaddon import Addon
# Kodi executes this context-menu file directly from resources/lib.  The
# add-on root is not always on sys.path (notably on Kodi 21 / Android), so
# import the neighboring module using the script directory explicitly.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
import video_queue
import library_cache

addon = Addon()
randomitems = addon.getSettingInt("randomitems")

# Smart playlist freshness uses the shared configurable cache lifetime.
PLAYLIST_ADD_BATCH_SIZE = 500
# This is a separate Kodi Python script, so it needs its own abort monitor.
CONTEXT_MONITOR = xbmc.Monitor()


def build_jsonrpc(method, params=None, rpc_id="1"):
    return json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": rpc_id
    })


def safe_execute(query):
    if CONTEXT_MONITOR.abortRequested():
        return {}
    response = xbmc.executeJSONRPC(query)
    try:
        return json.loads(response)
    except (ValueError, TypeError):
        xbmc.log(f"JSON decode error for query: {query}", xbmc.LOGERROR)
        return {}


def close_busy_dialog():
    xbmc.executebuiltin("Dialog.Close(busydialognocancel)")


def start_playlist(items, selected_item=None, source="random-library"):
    """Queue everything before playback, with the clicked item first when given."""
    if selected_item:
        # Place the explicitly selected library item first, independently of
        # the random library query (which may contain the selected item too).
        items = [selected_item] + [item for item in items if item != selected_item]
    items = list(items)[:max(1, randomitems)]
    if not items or CONTEXT_MONITOR.abortRequested():
        return False
    # Begin playback with the selected item immediately. Snapshot protection
    # lives in the service too, because context.py is a separate interpreter.
    started, complete = video_queue.start_queue(
        items, batch_size=PLAYLIST_ADD_BATCH_SIZE, monitor=CONTEXT_MONITOR, source=source, random_order=True)
    close_busy_dialog()
    if not complete:
        xbmc.log("Play Random: queue incomplete; previous saved resume retained",
                 xbmc.LOGWARNING)
    return started

def collect_ignored_ids(force=False, membership=None):
    """Map excluded (type, library id) to smart playlist, with short-lived cache."""
    if (not force and not addon.getSettingBool("IgnoreSmartPlaylistItems")) or CONTEXT_MONITOR.abortRequested():
        return {}

    cache_file = library_cache._paths()[1]
    old = {}
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError, TypeError):
        pass
    def unpack(data):
        try:
            return {(row[0], row[1]): row[2] for row in (data.get("rows", []) if isinstance(data, dict) else data)}
        except (TypeError, IndexError, KeyError):
            return {}
    previous = unpack(old)
    generation = library_cache.smart_generation()
    try:
        response = safe_execute(build_jsonrpc("Files.GetDirectory", {
            "directory": "library://video/playlists/", "properties": ["title"]}))
        if "result" not in response:
            raise RuntimeError("Could not enumerate smart playlists")
        paths = sorted({pl.get("file") for pl in response["result"].get("files", [])
                        if pl.get("file") and "kpr_xsp_metadata_test" not in pl["file"] and "kpr_internal_" not in pl["file"]})
        fingerprint = []
        for path in paths:
            stat = xbmcvfs.Stat(path)
            fingerprint.append([path, stat.st_mtime(), stat.st_size()])
        if (not force and isinstance(old, dict) and old.get("generation") == generation
                and old.get("fingerprint") == fingerprint
                and isinstance(old.get("memberships"), dict)
                and 0 <= time.time() - float(old.get("built_at", 0)) < library_cache.cache_max_age()):
            if membership is not None:
                membership.update(old["memberships"])
            return previous
        ignored_map = {}
        memberships = {}
        for path in paths:
            if CONTEXT_MONITOR.abortRequested():
                raise RuntimeError("Rebuild interrupted")
            response = safe_execute(build_jsonrpc("Files.GetDirectory", {
                "directory": path, "media": "video", "properties": ["title"]}))
            if "result" not in response:
                raise RuntimeError("Could not read smart playlist: " + path)
            members = memberships.setdefault(path, [])
            for item in response["result"].get("files", []):
                kind, item_id = item.get("type"), item.get("id")
                if kind in ("movie", "episode", "musicvideo") and type(item_id) is int:
                    ignored_map[(kind, item_id)] = path
                    members.append({"type": kind, "id": item_id})
        if CONTEXT_MONITOR.abortRequested():
            raise RuntimeError("Rebuild interrupted")
        with library_cache.CacheLock(cache_file, monitor=CONTEXT_MONITOR, timeout=5) as acquired:
            if not acquired:
                raise RuntimeError("Smart cache busy")
            library_cache._write(cache_file, {"built_at": time.time(), "generation": generation,
                "fingerprint": fingerprint, "memberships": memberships, "rows": [[k, i, pl] for (k, i), pl in ignored_map.items()]})
        xbmc.log("Play Random: smart-playlist cache rebuilt", xbmc.LOGINFO)
        if membership is not None:
            membership.update(memberships)
        return ignored_map
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        xbmc.log("Play Random smart cache rebuild failed; previous cache retained: " + str(error), xbmc.LOGWARNING)
        if force:
            raise
        return previous


def filter_items(items, item_type, ignored_ids, list_item, ignored_collector):
    result = []
    for it in items:
        item_id = it[f"{item_type}id"]
        key = (item_type, item_id)
        if key not in ignored_ids or (list_item and key == list_item):
            result.append({f"{item_type}id": item_id})
        else:
            ignored_collector.append(f"{item_type}:{item_id}")
    return result


def fetch_and_filter(method, result_key, item_type, ignored_ids, list_item,
                     ignored_collector, fetch_size=None, max_keep=None, params_extra=None):
    if max_keep is None:
        max_keep = randomitems
    if fetch_size is None:
        fetch_size = max_keep * 2

    params = {
        "limits": {"end": fetch_size},
        "sort": {"method": "random"}
        # No 'file' property needed: we queue by Kodi library ID.
    }
    if params_extra:
        params.update(params_extra)

    if CONTEXT_MONITOR.abortRequested():
        return []
    filters = params_extra or {}
    cached = library_cache.select(
        item_type, max_keep, ignored_ids=ignored_ids,
        tvshowid=filters.get("tvshowid"), season=filters.get("season"),
        monitor=CONTEXT_MONITOR)
    if cached is not None:
        return cached
    # If initial cache creation failed, keep the original Kodi query path.
    result = safe_execute(build_jsonrpc(method, params)).get("result", {}).get(result_key, [])
    filtered = filter_items(result, item_type, ignored_ids, list_item, ignored_collector)
    return filtered[:max_keep]


def run_context_menu():
    if CONTEXT_MONITOR.abortRequested():
        return
    selected_list_item = sys.listitem
    path = selected_list_item.getPath()
    dbid = xbmc.getInfoLabel("ListItem.DBID()")
    db_type = xbmc.getInfoLabel("ListItem.DBTYPE()")
    tvshow_dbid = xbmc.getInfoLabel("ListItem.TvShowDBID()")
    season = xbmc.getInfoLabel("ListItem.Season()")

    # The clicked item's video tag is more reliable in context-menu scripts
    # than global InfoLabels, which can refer to the focused list container.
    try:
        tag = selected_list_item.getVideoInfoTag()
        tagged_type = tag.getMediaType()
        tagged_dbid = tag.getDbId()
        if tagged_type in ("movie", "episode", "tvshow", "season", "musicvideo") and tagged_dbid > 0:
            db_type = tagged_type
            dbid = str(tagged_dbid)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass

    list_item_key = (db_type, int(dbid)) if str(dbid).isdigit() and int(dbid) > 0 else None

    xbmc.executebuiltin("ActivateWindow(busydialognocancel)")

    try:
        if path.startswith("videodb://") or (list_item_key and db_type in ("movie", "episode")):
            library_cache.ensure_fresh(CONTEXT_MONITOR)
            membership = {}
            ignored_ids = collect_ignored_ids(membership=membership)
            ignored_collector = []

            # A selected item from a smart playlist should continue within
            # that smart playlist, as in the original implementation.
            if list_item_key in ignored_ids:
                parent_playlist = ignored_ids[list_item_key]
                content = membership.get(parent_playlist)
                if content is None:
                    content = safe_execute(build_jsonrpc("Files.GetDirectory", {
                        "directory": parent_playlist, "media": "video", "properties": ["title"]
                    })).get("result", {}).get("files", [])
                remaining, target_start_item = [], None
                for item in content:
                    formatted_item = {f"{item['type']}id": item['id']}
                    if (item["type"], item["id"]) == list_item_key:
                        target_start_item = formatted_item
                    else:
                        remaining.append(formatted_item)

                if target_start_item:
                    start_playlist([target_start_item] + remaining,
                                   selected_item=target_start_item, source="smart-playlist: " + parent_playlist)
                return

            if db_type == "movie":
                first_item = {"movieid": list_item_key[1]} if list_item_key else None
                fetched = fetch_and_filter(
                    "VideoLibrary.GetMovies", "movies", "movie",
                    ignored_ids, list_item_key, ignored_collector
                )
                if first_item:
                    start_playlist(fetched, selected_item=first_item)

            elif db_type == "episode":
                first_item = {"episodeid": list_item_key[1]} if list_item_key else None
                params_extra = {}
                if tvshow_dbid.isdigit():
                    params_extra["tvshowid"] = int(tvshow_dbid)
                    if season.isdigit() and int(season) > 0:
                        params_extra["season"] = int(season)

                fetched = fetch_and_filter(
                    "VideoLibrary.GetEpisodes", "episodes", "episode",
                    ignored_ids, list_item_key, ignored_collector,
                    params_extra=params_extra
                )
                if first_item:
                    start_playlist(fetched, selected_item=first_item)

            elif db_type in ("season", "tvshow"):
                tvshow_id = (xbmc.getInfoLabel("ListItem.TvShowDBID()")
                             if db_type == "tvshow" else tvshow_dbid)
                if not tvshow_id or not str(tvshow_id).isdigit():
                    tvshow_id = dbid if db_type == "tvshow" else None

                if tvshow_id and str(tvshow_id).isdigit():
                    params_extra = {"tvshowid": int(tvshow_id)}
                    if db_type == "season" and season.isdigit() and int(season) > 0:
                        params_extra["season"] = int(season)

                    # One randomized lookup, then queue the entire result.
                    # Playing before this step saves an incomplete resume queue.
                    fetched = fetch_and_filter(
                        "VideoLibrary.GetEpisodes", "episodes", "episode",
                        ignored_ids, None, ignored_collector,
                        fetch_size=max(20, randomitems * 2),
                        max_keep=randomitems, params_extra=params_extra
                    )
                    if fetched:
                        start_playlist(fetched)

        elif path.startswith("library://"):
            safe_execute(build_jsonrpc("Player.Open", {
                "item": {"recursive": True, "directory": path},
                "options": {"shuffled": True}
            }))
    finally:
        close_busy_dialog()


if __name__ == "__main__":
    run_context_menu()
