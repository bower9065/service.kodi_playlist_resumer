import sys
import xbmc
import json
import os
import xbmcvfs
from urllib.parse import parse_qs
from xbmcaddon import Addon
import traceback
import time

addon = Addon()
ADDON_VERSION = addon.getAddonInfo('version')
ADDON_NAME = addon.getAddonInfo('name')
randomitems = addon.getSettingInt("randomitems")

def log(message, exception_instance=None, level=xbmc.LOGDEBUG):
    """
    Log a message to the Kodi debug log, if debug logging is turned on.
    """
    message = f'### {ADDON_NAME} {ADDON_VERSION} - {message}'
    if exception_instance:
        message += f' ### Exception: {traceback.format_exc(exception_instance)}'
    xbmc.log(message, level)

def build_jsonrpc(method, params=None, rpc_id="1"):
    return json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": rpc_id
    })

def safe_execute(query):
    response = xbmc.executeJSONRPC(query)
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        log(f"JSON decode error for query: {query}", xbmc.LOGERROR)
        return {}

def collect_playlist_items():
    ignored = set()
    playlist_dir = xbmcvfs.translatePath("special://userdata/library/video/playlists/")
    if not os.path.exists(playlist_dir):
        log("No playlist directory found.")
        return ignored

    query = build_jsonrpc("Files.GetDirectory", {
        "directory": "library://video/playlists/",
        "properties": ["title"]
    })
    result = safe_execute(query).get("result", {}).get("files", [])
    log(f"Found {len(result)} playlists in directory.")

    for pl in result:
        playlist_path = pl["file"]
        q = build_jsonrpc("Files.GetDirectory", {
            "directory": playlist_path,
            "properties": ["title"]
        })
        items = safe_execute(q).get("result", {}).get("files", [])
        log(f"Playlist '{playlist_path}' contains {len(items)} items.")
        for item in items:
            item_type = item.get("type")
            item_id = item.get("id")
            if item_type and item_id:
                ignored.add((item_type, item_id))

    log(f"Total ignored items collected: {len(ignored)}")
    return ignored

def find_playlist_for_item(db_type, dbid):
    playlist_dir = xbmcvfs.translatePath("special://userdata/library/video/playlists/")
    if not os.path.exists(playlist_dir):
        return None

    query = build_jsonrpc("Files.GetDirectory", {
        "directory": "library://video/playlists/",
        "properties": ["title"]
    })
    result = safe_execute(query).get("result", {}).get("files", [])
    for pl in result:
        playlist_path = pl["file"]
        q = build_jsonrpc("Files.GetDirectory", {
            "directory": playlist_path,
            "properties": ["title"]
        })
        items = safe_execute(q).get("result", {}).get("files", [])
        for item in items:
            if item.get("type") == db_type and item.get("id") == int(dbid):
                return playlist_path
    return None

def fetch_and_filter(method, result_key, item_type, ignored_ids,
                     fetch_size=None, max_keep=None, params_extra=None):
    if max_keep is None:
        max_keep = randomitems
    if fetch_size is None:
        fetch_size = max_keep * 2

    params = {
        "limits": {"end": fetch_size},
        "sort": {"method": "random"},
        "properties": ["file"]
    }
    if params_extra:
        params.update(params_extra)

    query = build_jsonrpc(method, params)
    result = safe_execute(query).get("result", {}).get(result_key, [])
    log(f"Fetched {len(result)} {item_type}s before filtering.")

    filtered = []
    for it in result:
        item_id = it.get(f"{item_type}id")
        if item_id and (item_type, item_id) not in ignored_ids:
            filtered.append({f"{item_type}id": item_id})

    log(f"Filtered list ({len(filtered)} items): {filtered}")
    return filtered[:max_keep]

if __name__ == "__main__":
    path = sys.listitem.getPath()
    dbid = xbmc.getInfoLabel("ListItem.DBID()")
    db_type = xbmc.getInfoLabel("ListItem.DBTYPE()")
    tvshow_dbid = xbmc.getInfoLabel("ListItem.TvShowDBID()")
    path_type, db_path = path.split("://", 1)
    db_path = db_path.split("?", 1)
    query = parse_qs(db_path[1]) if len(db_path) > 1 else None
    db_path = db_path[0].rstrip("/").split("/")

    xbmc.executebuiltin("ActivateWindow(busydialognocancel)")
    try:
        log(f"Started with path_type={path_type}, db_type={db_type}, dbid={dbid}, tvshow_dbid={tvshow_dbid}")

        if path_type == "videodb":
            playlist_path = find_playlist_for_item(db_type, dbid)
            if playlist_path:
                play_query = build_jsonrpc("Player.Open", {
                    "item": {"recursive": True, "directory": playlist_path}
                })
                safe_execute(play_query)
                log(f"Playing from playlist: {playlist_path}")
            else:
                log("Item not found in any playlist — collecting ignored items.")
                ignored_ids = collect_playlist_items()

                def fetch_items(count, tvshow=False):
                    if db_type == "movie":
                        return fetch_and_filter("VideoLibrary.GetMovies", "movies", "movie",
                                                ignored_ids, max_keep=count)
                    elif db_type == "episode":
                        params_extra = {"tvshowid": int(tvshow_dbid)} if tvshow_dbid.isdigit() else None
                        return fetch_and_filter("VideoLibrary.GetEpisodes", "episodes", "episode",
                                                ignored_ids, max_keep=count, params_extra=params_extra)
                    return []

                # Step 1: Fetch and play 1 video
                first_item = fetch_items(1)
                if not first_item:
                    log("No videos found to play after filtering.")
                else:
                    log(f"Playing first random item: {first_item}")
                    xbmc.executeJSONRPC(build_jsonrpc("Playlist.Clear", {"playlistid": 1}))
                    xbmc.executeJSONRPC(build_jsonrpc("Playlist.Add", {"playlistid": 1, "item": first_item[0]}))
                    xbmc.sleep(100)
                    xbmc.executeJSONRPC(build_jsonrpc("Player.Open", {"item": {"playlistid": 1, "position": 0}}))

                    # Step 2: Wait briefly for playback to start
                    for _ in range(10):
                        if xbmc.getCondVisibility("Player.HasVideo"):
                            log("Playback started — now fetching more items.")
                            break
                        xbmc.sleep(500)

                    # Step 3: Fetch more random videos and queue them
                    remaining_items = fetch_items(randomitems - 1)
                    if remaining_items:
                        log(f"Adding {len(remaining_items)} additional random items to playlist.")
                        for item in remaining_items:
                            xbmc.executeJSONRPC(build_jsonrpc("Playlist.Add", {"playlistid": 1, "item": item}))
                    else:
                        log("No additional videos found to queue.")

        elif path_type == "library":
            query = build_jsonrpc("Player.Open", {
                "item": {"recursive": True, "directory": path}
            })
            safe_execute(query)
            log(f"Playing random videos from library path: {db_path[2]}")

    except Exception as e:
        log("Error during random playback setup.", e, xbmc.LOGERROR)
    finally:
        xbmc.executebuiltin("Dialog.Close(busydialognocancel)")
        log("Execution finished.")
