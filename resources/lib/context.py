import sys
import xbmc
import xbmcgui
import json
import os
import xbmcvfs
from xbmcaddon import Addon

addon = Addon()
randomitems = addon.getSettingInt("randomitems")

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
        xbmc.log(f"JSON decode error for query: {query}", xbmc.LOGERROR)
        return {}

def collect_ignored_ids():
    """Returns a dictionary mapping (type, id) to the playlist path it belongs to."""
    ignored_map = {}
    if not addon.getSettingBool("IgnoreSmartPlaylistItems"):
        return ignored_map

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
            ignored_map[(item["type"], item["id"])] = playlist_path

    return ignored_map

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
        "sort": {"method": "random"},
        "properties": ["file"]
    }
    if params_extra:
        params.update(params_extra)

    result = safe_execute(build_jsonrpc(method, params)).get("result", {}).get(result_key, [])
    filtered = filter_items(result, item_type, ignored_ids, list_item, ignored_collector)
    return filtered[:max_keep]

if __name__ == "__main__":
    path = sys.listitem.getPath()
    dbid = xbmc.getInfoLabel("ListItem.DBID()")
    db_type = xbmc.getInfoLabel("ListItem.DBTYPE()")
    tvshow_dbid = xbmc.getInfoLabel("ListItem.TvShowDBID()")
    season = xbmc.getInfoLabel('ListItem.Season()')

    list_item_key = (db_type, int(dbid)) if dbid.isdigit() else None

    xbmc.executebuiltin("ActivateWindow(busydialognocancel)")

    try:
        if path.startswith("videodb://"):
            ignored_ids = collect_ignored_ids()
            ignored_collector = []

            # --- REDIRECTION LOGIC FOR IGNORED ITEMS ---
            if list_item_key in ignored_ids:
                parent_playlist = ignored_ids[list_item_key]
                q = build_jsonrpc("Files.GetDirectory", {
                    "directory": parent_playlist,
                    "properties": ["title"]
                })
                playlist_content = safe_execute(q).get("result", {}).get("files", [])
                
                queue_items = []
                target_start_item = None
                
                for item in playlist_content:
                    formatted_item = {f"{item['type']}id": item['id']}
                    if (item['type'], item['id']) == list_item_key:
                        target_start_item = formatted_item
                    else:
                        queue_items.append(formatted_item)

                if target_start_item:
                    safe_execute(build_jsonrpc("Playlist.Clear", {"playlistid": 1}))
                    safe_execute(build_jsonrpc("Playlist.Add", {"playlistid": 1, "item": target_start_item}))
                    xbmc.sleep(200)
                    safe_execute(build_jsonrpc("Player.Open", {"item": {"playlistid": 1, "position": 0}}))
                    xbmc.executebuiltin("Dialog.Close(busydialognocancel)") # Close dialog early
                    
                    for remaining in queue_items:
                        safe_execute(build_jsonrpc("Playlist.Add", {"playlistid": 1, "item": remaining}))
                sys.exit()

            # --- NORMAL PLAYBACK LOGIC ---
            if db_type == "movie":
                first_item = {"movieid": list_item_key[1]} if list_item_key else None
                fetched = fetch_and_filter("VideoLibrary.GetMovies", "movies", "movie", ignored_ids, list_item_key, ignored_collector)
                
                if first_item:
                    fetched = [m for m in fetched if m.get("movieid") != first_item["movieid"]]
                    safe_execute(build_jsonrpc("Playlist.Clear", {"playlistid": 1}))
                    safe_execute(build_jsonrpc("Playlist.Add", {"playlistid": 1, "item": first_item}))
                    xbmc.sleep(200)
                    safe_execute(build_jsonrpc("Player.Open", {"item": {"playlistid": 1, "position": 0}}))
                    xbmc.executebuiltin("Dialog.Close(busydialognocancel)") # Close dialog early

                for item in fetched:
                    safe_execute(build_jsonrpc("Playlist.Add", {"playlistid": 1, "item": item}))

            elif db_type == "episode":
                first_item = {"episodeid": list_item_key[1]} if list_item_key else None
                params_extra = {}
                if tvshow_dbid.isdigit():
                    params_extra["tvshowid"] = int(tvshow_dbid)
                    if season.isdigit() and int(season) > 0:
                        params_extra["season"] = int(season)

                fetched = fetch_and_filter("VideoLibrary.GetEpisodes", "episodes", "episode", ignored_ids, list_item_key, ignored_collector, params_extra=params_extra)
                
                if first_item:
                    fetched = [e for e in fetched if e.get("episodeid") != first_item["episodeid"]]
                    safe_execute(build_jsonrpc("Playlist.Clear", {"playlistid": 1}))
                    safe_execute(build_jsonrpc("Playlist.Add", {"playlistid": 1, "item": first_item}))
                    xbmc.sleep(200)
                    safe_execute(build_jsonrpc("Player.Open", {"item": {"playlistid": 1, "position": 0}}))
                    xbmc.executebuiltin("Dialog.Close(busydialognocancel)") # Close dialog early

                for item in fetched:
                    safe_execute(build_jsonrpc("Playlist.Add", {"playlistid": 1, "item": item}))

            elif db_type in ("season", "tvshow"):
                tvshow_id = xbmc.getInfoLabel("ListItem.TvShowDBID()") if db_type == "tvshow" else tvshow_dbid
                if not tvshow_id or not str(tvshow_id).isdigit():
                    tvshow_id = dbid if db_type == "tvshow" else None

                if tvshow_id:
                    params_extra = {"tvshowid": int(tvshow_id)}
                    if db_type == "season" and season.isdigit() and int(season) > 0:
                        params_extra["season"] = int(season)
                    fetched = fetch_and_filter("VideoLibrary.GetEpisodes", "episodes", "episode", ignored_ids, None, ignored_collector, params_extra=params_extra)
                    if fetched:
                        first_item = fetched.pop(0)
                        safe_execute(build_jsonrpc("Playlist.Clear", {"playlistid": 1}))
                        safe_execute(build_jsonrpc("Playlist.Add", {"playlistid": 1, "item": first_item}))
                        xbmc.sleep(200)
                        safe_execute(build_jsonrpc("Player.Open", {"item": {"playlistid": 1, "position": 0}}))
                        xbmc.executebuiltin("Dialog.Close(busydialognocancel)") # Close dialog early
                        
                        for item in fetched:
                            safe_execute(build_jsonrpc("Playlist.Add", {"playlistid": 1, "item": item}))

        elif path.startswith("library://"):
            safe_execute(build_jsonrpc("Player.Open", {"item": {"recursive": True, "directory": path}, "options": {"shuffled": True}}))
            xbmc.executebuiltin("Dialog.Close(busydialognocancel)")

    finally:
        # Final safety check to close dialog if it's still open
        xbmc.executebuiltin("Dialog.Close(busydialognocancel)")
