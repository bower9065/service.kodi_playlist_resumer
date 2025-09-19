import sys
import xbmc
import json
import os
import xbmcvfs
from urllib.parse import parse_qs
from xbmcaddon import Addon


def build_jsonrpc(method, params=None, rpc_id="1"):
    """Helper to build JSON-RPC requests."""
    return json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": rpc_id
    })


def safe_execute(query):
    """Executes a JSON-RPC query and returns parsed JSON result."""
    response = xbmc.executeJSONRPC(query)
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        xbmc.log(f"JSON decode error for query: {query}", xbmc.LOGERROR)
        return {}


def collect_ignored_ids():
    """Return a set of (type, id) for items inside smart playlists."""
    ignored = set()
    if not Addon().getSettingBool("IgnoreSmartPlaylistItems"):
        return ignored

    playlist_dir = xbmcvfs.translatePath("special://userdata/library/video/playlists/")
    if not os.path.exists(playlist_dir):
        return ignored

    query = build_jsonrpc("Files.GetDirectory", {"properties": ["title"], "directory": "library://video/playlists/"})
    result = safe_execute(query).get("result", {}).get("files", [])

    for pl in result:
        playlist_path = pl["file"]
        q = build_jsonrpc("Files.GetDirectory", {"properties": ["title"], "directory": playlist_path})
        items = safe_execute(q).get("result", {}).get("files", [])
        for item in items:
            ignored.add((item["type"], item["id"]))

    return ignored


def filter_items(items, item_type, ignored_ids, list_item, ignored_collector):
    """Filter playlist items, excluding ignored ones unless it's the current item."""
    result = []
    for it in items:
        item_id = it[f"{item_type}id"]
        key = (item_type, item_id)
        if key not in ignored_ids or list_item == key:
            result.append({f"{item_type}id": item_id})
        else:
            ignored_collector.append(f"{item_type}:{item_id}")
    return result


def fetch_and_filter(method, result_key, item_type, ignored_ids, list_item, ignored_collector, fetch_size=1000, max_keep=100, params_extra=None):
    """Fetch items, filter them, and return up to `max_keep` valid results."""
    params = {
        "limits": {"end": fetch_size},
        "sort": {"method": "random"},
        "properties": ["file"]
    }
    if params_extra:
        params.update(params_extra)

    query = build_jsonrpc(method, params)
    result = safe_execute(query).get("result", {}).get(result_key, [])
    filtered = filter_items(result, item_type, ignored_ids, list_item, ignored_collector)
    return filtered[:max_keep]


if __name__ == "__main__":
    path = sys.listitem.getPath()
    dbid = xbmc.getInfoLabel("ListItem.DBID()")
    db_type = xbmc.getInfoLabel("ListItem.DBTYPE()")
    tvshow_title = xbmc.getInfoLabel("ListItem.TVShowTitle()")
    tvshow_dbid = xbmc.getInfoLabel("ListItem.TvShowDBID()")
    tvshow_season = "Season " + xbmc.getInfoLabel("ListItem.Season()")

    path_type, db_path = path.split("://", 1)
    db_path = db_path.split("?", 1)
    query = parse_qs(db_path[1]) if len(db_path) > 1 else None
    db_path = db_path[0].rstrip("/").split("/")

    list_item = (db_type, int(dbid)) if dbid.isdigit() else None

    xbmc.executebuiltin("ActivateWindow(busydialognocancel)")

    try:
        if path_type == "videodb":
            ignored_ids = collect_ignored_ids()
            ignored_collector = []
            playlist_items = []

            if db_type == "movie":
                playlist_items = fetch_and_filter(
                    "VideoLibrary.GetMovies", "movies", "movie",
                    ignored_ids, list_item, ignored_collector
                )

            elif db_type == "episode":
                playlist_items = fetch_and_filter(
                    "VideoLibrary.GetEpisodes", "episodes", "episode",
                    ignored_ids, list_item, ignored_collector,
                    params_extra={"tvshowid": int(tvshow_dbid)} if tvshow_dbid.isdigit() else None
                )

            elif db_type == "musicvideo":
                playlist_items = fetch_and_filter(
                    "VideoLibrary.GetMusicVideos", "musicvideos", "musicvideo",
                    ignored_ids, list_item, ignored_collector
                )

            elif db_type == "tvshow":
                playlist_items = fetch_and_filter(
                    "VideoLibrary.GetEpisodes", "episodes", "episode",
                    ignored_ids, list_item, ignored_collector,
                    params_extra={"tvshowid": int(dbid)} if dbid.isdigit() else None
                )

            elif db_type == "season":
                playlist_items = fetch_and_filter(
                    "VideoLibrary.GetEpisodes", "episodes", "episode",
                    ignored_ids, list_item, ignored_collector,
                    params_extra={"season": int(xbmc.getInfoLabel('ListItem.Season()')), "tvshowid": int(tvshow_dbid)} if tvshow_dbid.isdigit() else None
                )

            # Single log line with all ignored items
            if ignored_collector:
                xbmc.log("----(Playlist Resumer)...IGNORED VIDEOS " + ", ".join(ignored_collector), xbmc.LOGINFO)

            # Add playlist and play
            if playlist_items:
                clear = build_jsonrpc("Playlist.Clear", {"playlistid": 1}, "playlist_clear")
                add = build_jsonrpc("Playlist.Add", {"playlistid": 1, "item": playlist_items}, "playlist_add")
                play = build_jsonrpc("Player.Open", {"item": {"playlistid": 1, "position": 0}}, "player_open")

                safe_execute(clear)
                safe_execute(add)
                xbmc.sleep(100)
                safe_execute(play)

        elif path_type == "library":
            query = build_jsonrpc("Player.Open", {
                "item": {"recursive": True, "directory": path},
                "options": {"shuffled": True}
            }, "play_playlist")
            safe_execute(query)
            xbmc.log(f"----(Playlist Resumer)...Playing random videos from {db_path[2]}", xbmc.LOGINFO)

    finally:
        xbmc.executebuiltin("Dialog.Close(busydialognocancel)")
