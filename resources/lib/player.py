from random import randint
from .common import *
from .store import Store
import json
import time
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

    def onPlayBackPaused(self):
        log('onPlayBackPaused')
        Store.paused_time = time.time()
        log(f'Playback paused at: {Store.paused_time}')

    def onPlayBackEnded(self):  # video ended normally (user didn't stop it)
        log("onPlayBackEnded")
        self.update_resume_point(-1)
        self.autoplay_random_if_enabled()

    def onPlayBackStopped(self):  # user stopped / shutdown
        log("Attempting onPlayBackStopped")
        xbmc.sleep(3000)
        if Store.just_woke or Store.just_suspend:
            log(f"Skipping onPlayBackStopped: just_woke={Store.just_woke}, just_suspend={Store.just_suspend}")
            return
        else:    
            log(f"NOT Skipping onPlayBackStopped: just_woke={Store.just_woke}, just_suspend={Store.just_suspend}")
            self.update_resume_point(-2)

    def onAVStarted(self):
        log("onAVStarted")
        if Store.just_woke or Store.just_suspend:
            log(f"Delaying onAVStarted: just_woke={Store.just_woke}, just_suspend={Store.just_suspend}")
            for _ in range(30):  
                if Store.kodi_event_monitor.abortRequested():
                    return
                xbmc.sleep(1000)
            Store.just_woke = False
            Store.just_suspend = False
        if not self.isPlayingVideo():
            log("onAVStarted but is Not playing a video - skipping: ")
            return
        Store.clear_old_play_details()
        xbmc.sleep(1500)
        Store.update_current_playing_file_path(self.getPlayingFile())
        Store.length_of_currently_playing_file = self.getTotalTime()
        try:
            self.update_resume_point(self.getTime())
        except RuntimeError:
            log('Could not get current playback time from player')
        while self.isPlaying() and not Store.kodi_event_monitor.abortRequested():
            if not xbmc.getCondVisibility('Player.Paused'):
                try:
                    self.update_resume_point(self.getTime())
                except RuntimeError:
                    log('Could not get current playback time from player')
            for i in range(0, Store.save_interval_seconds):
                if Store.kodi_event_monitor.abortRequested() or not self.isPlaying():
                    return
                xbmc.sleep(1000)

    def onPlayBackSeek(self, time, seekOffset):
        log(f'onPlayBackSeek time {time}, seekOffset {seekOffset}')
        try:
            self.update_resume_point(self.getTime())
        except RuntimeError:
            log("Could not get playing time - seeked past end?")
            self.update_resume_point(0)
            pass
            Store.just_woke = False
            Store.just_suspend = False
    def onPlayBackSeekChapter(self, chapter):
        log(f'onPlayBackSeekChapter chapter: {chapter}')
        try:
            self.update_resume_point(self.getTime())
        except RuntimeError:
            log("Could not get playing time - seeked past end?")
            self.update_resume_point(0)
            pass
            Store.just_woke = False
            Store.just_suspend = False
    def update_resume_point(self, seconds):
        """
        This is where the work is done - stores a new resume point in the Kodi library for the currently playing file

        :param: seconds: the time to update the resume point to.  @todo add notes on -1, -2 etc here!
        :param: Store.library_id: the Kodi library id of the currently playing file
        :return: None
        """
        seconds = int(seconds)    
        if not Store.currently_playing_file_path:
            log("No valid currently_playing_file_path found - therefore not setting resume point")
            return
        if Store.library_id and Store.library_id < 0:
            log(f"No/invalid library id ({Store.library_id}) for {Store.currently_playing_file_path}")
            return
        if seconds == -2:
            if not Store.kodi_event_monitor.abortRequested():                         
                if Store.resume_if_stopped:
                    log("Video was stopped by user with Resume if Stopped on, and will keep our resume point")   
                    return
                else:
                    log("Video was stopped by user with Resume if Stopped off, and will remove our resume point")                  
                    Store.clear_old_play_details()
                    xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Playlist.Clear","params":{"playlistid":1},"id":"playlist_clear"}')
                    return
            else:
                log("Video was stopped as a result of Abort Requested, and will keep our resume point")   
                return      
        if seconds == -1:
            log("Removing resume points because the file has ended normally")
            Store.clear_old_play_details()
            return         
        # if current time < Kodi's ignoresecondsatstart setting, save as 0 seconds
        if 0 < seconds < Store.ignore_seconds_at_start:
            log(f'Resume point ({seconds}) is below Kodi\'s ignoresecondsatstart'
                f' setting of {Store.ignore_seconds_at_start}, setting resume point to 0')
            seconds = 0
            with open(Store.file_to_store_resume_point, 'w') as f:
                f.write(str(seconds))
        # if current time > Kodi's ignorepercentatend setting, save resume point
        percent_played = int((seconds * 100) / Store.length_of_currently_playing_file)
        if percent_played > (100 - Store.ignore_percent_at_end):
            log(f'Resume point as current percent played ({percent_played}) is above Kodi\'s ignorepercentatend'
                f' setting of {Store.ignore_percent_at_end}, setting resume point to 0')
            seconds = 0
            with open(Store.file_to_store_resume_point, 'w') as f:
                f.write(str(seconds))
        # if seconds between start ignore and end ignore, save resume point
        if percent_played < (100 - Store.ignore_percent_at_end) and seconds > Store.ignore_seconds_at_start :
            log(f'Setting custom resume seconds to {seconds}')
            with open(Store.file_to_store_resume_point, 'w') as f:
                f.write(str(seconds))                             
        if seconds == 0:
            log(f'Removing resume point for: {Store.currently_playing_file_path}, type: {Store.type_of_video}, library id: {Store.library_id}')
        else:
            log(f'Setting resume point for: {Store.currently_playing_file_path}, type: {Store.type_of_video}, library id: {Store.library_id}, to: {seconds} seconds')
        id_name = None
        if Store.type_of_video == 'episode':
            method = 'VideoLibrary.SetEpisodeDetails'
            get_method = 'VideoLibrary.GetEpisodeDetails'
            id_name = 'episodeid'
        elif Store.type_of_video == 'movie':
            method = 'VideoLibrary.SetMovieDetails'
            get_method = 'VideoLibrary.GetMovieDetails'
            id_name = 'movieid'
        elif Store.type_of_video == 'musicvideo':
            method = 'VideoLibrary.SetMusicVideoDetails'
            get_method = 'VideoLibrary.GetMusicVideoDetails'
            id_name = 'musicvideoid'
        else:
            log(f'Did not recognise type of video [{Store.type_of_video}] - assume non-library video')
            method = 'Files.SetFileDetails'
            get_method = 'Files.GetFileDetails'
        json_dict = {
            "jsonrpc": "2.0",
            "id": "setResumePoint",
            "method": method,
        }
        if id_name:
            params = {
                id_name: Store.library_id,
                "resume": {
                    "position": seconds,
                    "total": Store.length_of_currently_playing_file
                }
            }
        else:
            params = {
                "file": Store.currently_playing_file_path,
                "media": "video",
                "resume": {
                    "position": seconds,
                    "total": Store.length_of_currently_playing_file
                }
            }
        json_dict['params'] = params
        query = json.dumps(json_dict)
        send_kodi_json(f'Set resume point for: {Store.currently_playing_file_path}, type: {Store.type_of_video}, id: {Store.library_id}, to: {seconds} seconds, total: {Store.length_of_currently_playing_file}', query)
        json_dict = {
            "jsonrpc": "2.0",
            "id": "getResumePoint",
            "method": get_method,
        }
        if id_name:
            params = {
                id_name: Store.library_id,
                "properties": ["resume"],
            }
        else:
            params = {
                "file": Store.currently_playing_file_path,
                "media": "video",
                "properties": ["resume"],
            }
        json_dict['params'] = params
        query = json.dumps(json_dict)
        send_kodi_json(f'Check new resume point & total for: {Store.currently_playing_file_path}, type: {Store.type_of_video}, id: {Store.library_id}', query)

    def resume_if_was_playing(self):
        """
        Automatically resume a video after a crash.
        Restores playlist by:
        1) Adding first item
        2) Starting playback
        3) Seeking
        4) Adding remaining items
        """

        if self.isPlaying():
            log("Resume stopped: already playing")
            return False

        monitor = xbmc.Monitor()

        if not (
            Store.resume_on_startup
            and os.path.exists(Store.file_to_store_resume_point)
            and os.path.exists(Store.file_to_store_playlist_items)
            and os.path.exists(Store.file_to_store_playlist_position)
        ):
            log("Resume stopped: missing resume files")
            return False

        with open(Store.file_to_store_playlist_items, "r") as f:
            items_json = f.read()

        if items_json == "[]":
            log("Resume stopped: empty playlist")
            return False

        # Idle delay logic (unchanged, but compact)
        if not monitor.waitForAbort(max(int(Store.resume_delay) - 4, 0)):
            if xbmc.getGlobalIdleTime() >= int(Store.resume_delay) - 4:
                notify("Preparing to resume playback...", xbmcgui.NOTIFICATION_INFO)
                monitor.waitForAbort(4)
            else:
                log("Resume canceled: idle time pre-check failed")
                return True

        if xbmc.getGlobalIdleTime() < int(Store.resume_delay):
            log("Resume canceled: idle time final check failed")
            return True

        notify("Resuming playback...", xbmcgui.NOTIFICATION_INFO)
        xbmc.executebuiltin("ActivateWindow(busydialognocancel)")

        # Read resume point
        try:
            with open(Store.file_to_store_resume_point, "r") as f:
                resume_point = float(f.read())
        except Exception:
            log("Invalid resume point file")
            xbmc.executebuiltin("Dialog.Close(busydialognocancel)")
            return False

        # ---- FILEPATH PLAYBACK ----
        if not any(k in items_json for k in ("movieid", "episodeid", "musicvideoid")):
            log("Resuming via filepath")
            self.play(items_json)
            xbmc.executebuiltin("Dialog.Close(busydialognocancel)")
            return True

        # ---- PLAYLIST PLAYBACK ----
        items = json.loads(items_json)

        with open(Store.file_to_store_playlist_position, "r") as f:
            position = int(f.read())

        first_item = items[position]
        remaining_items = items[position + 1 :]

        # Clear playlist
        xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0",
            "method": "Playlist.Clear",
            "params": {"playlistid": 1},
            "id": 1
        }))

        # Add first item only
        xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0",
            "method": "Playlist.Add",
            "params": {"playlistid": 1, "item": first_item},
            "id": 2
        }))

        # Start playback
        xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0",
            "method": "Player.Open",
            "params": {"item": {"playlistid": 1, "position": 0}},
            "id": 3
        }))
        
        xbmc.executebuiltin("Dialog.Close(busydialognocancel)")
        
        # Wait for player readiness
        waited = 0
        while (
            not self.isPlayingVideo()
            or self.getTotalTime() <= 0
        ) and not monitor.abortRequested() and waited < 50:
            monitor.waitForAbort(0.2)
            waited += 1

        # Seek AFTER playback starts
        offset = resume_point - int(Store.resume_offset)
        self.seekTime(offset if offset > 0 else resume_point)
        log("Seek completed")

        # Add remaining items AFTER seek
        for item in remaining_items:
            xbmc.executeJSONRPC(json.dumps({
                "jsonrpc": "2.0",
                "method": "Playlist.Add",
                "params": {"playlistid": 1, "item": item},
                "id": 4
            }))

        log("Remaining playlist items added")

        xbmc.executebuiltin("Dialog.Close(busydialognocancel)")
        return True


    def get_random_library_video(self, first_only=False, tvshow_id=None):
        """
        Get random videos from the library.
        If first_only=True, fetch only 1 item for immediate playback.
        If tvshow_id is provided, fetch episodes only from that TV show.
        """
        xbmc.executebuiltin('ActivateWindow(busydialognocancel)')
        enabled_types = []
        if get_setting_as_bool("randomtv") and Store.video_types_in_library.get("episodes"):
            enabled_types.append(("episodes", "GetEpisodes"))
        if get_setting_as_bool("randommovies") and Store.video_types_in_library.get("movies"):
            enabled_types.append(("movies", "GetMovies"))
        if get_setting_as_bool("randommusicvideos") and Store.video_types_in_library.get("musicvideos"):
            enabled_types.append(("musicvideos", "GetMusicVideos"))

        if not enabled_types:
            log("No enabled video types in library. Cannot autoplay random video.")
            return []

        # If we're fetching episodes from a specific TV show, override result_type/method
        if tvshow_id:
            result_type, method = "episodes", "GetEpisodes"
        else:
            result_type, method = enabled_types[randint(0, len(enabled_types)-1)]

        log(f'Getting a random video from: {result_type}')

        limit_count = 1 if first_only else Store.randomitems
        query_params = {
            "limits": {"end": limit_count},
            "sort": {"method": "random"},
            "properties": ["file"]
        }
        if tvshow_id:
            query_params["tvshowid"] = tvshow_id

        query = {
            "jsonrpc": "2.0",
            "id": "randomLibraryVideo",
            "method": f"VideoLibrary.{method}",
            "params": query_params
        }

        json_response = json.loads(xbmc.executeJSONRPC(json.dumps(query)))

        ignored_ids = set()
        if Addon().getSettingBool("IgnoreSmartPlaylistItems"):
            playlists_dir = xbmcvfs.translatePath("special://userdata/library/video/playlists/")
            if os.path.exists(playlists_dir):
                playlist_list = json.loads(xbmc.executeJSONRPC(json.dumps({
                    "jsonrpc":"2.0",
                    "method":"Files.GetDirectory",
                    "params":{"properties": ["title"], "directory":"library://video/playlists/"},
                    "id":"get_directory"
                })))['result']['files']
                for pl in playlist_list:
                    playlist_items = json.loads(xbmc.executeJSONRPC(json.dumps({
                        "jsonrpc":"2.0",
                        "method":"Files.GetDirectory",
                        "params":{"properties": ["title"], "directory": pl["file"]},
                        "id":"get_directory"
                    })))['result']['files']
                    for item in playlist_items:
                        ignored_ids.add((item['type'], item['id']))
            else:
                log("Playlist directory does not exist")
        else:
            log("Ignore Smart Playlist Items is turned off")

        video_list = json_response['result'].get(result_type, [])
        random_list = []
        for item in video_list:
            vid_id = item[f"{result_type[:-1]}id"]
            if (result_type[:-1], vid_id) not in ignored_ids:
                random_list.append({f"{result_type[:-1]}id": vid_id})

        if not random_list:
            log(f"All {result_type} are ignored. Trying another type...")
            Store.video_types_in_library[result_type] = False
            return self.get_random_library_video(first_only, tvshow_id)

        return random_list


    def autoplay_random_if_enabled(self):
        """
        Play a random video if enabled, fetching one first for immediate playback,
        then fetching the rest to append to the playlist.
        """
        if not Store.autoplay_random:
            log("Auto Play Random stopped, turned off.")
            return

        if self.isPlaying():
            log("Auto Play Random stopped, video is already playing.")
            return

        xbmc.log('----(Playlist Resumer)...Preparing to play random videos.', xbmc.LOGINFO)
        xbmc.sleep((int(Store.random_delay) - 4) * 1000)

        if xbmc.getGlobalIdleTime() >= (int(Store.random_delay) - 4):
            notify('Preparing to play random videos...', xbmcgui.NOTIFICATION_INFO)
            xbmc.sleep(4000)

        if xbmc.getGlobalIdleTime() < int(Store.random_delay):
            notify('Play random videos canceled', xbmcgui.NOTIFICATION_INFO)
            log("Auto Play Random stopped, idle time failed.")
            return

        notify('Playing random videos...', xbmcgui.NOTIFICATION_INFO)
        log("Auto Play Random starting, idle time passed")

        # Get the first random video
        first_item = self.get_random_library_video(first_only=True)
        if not first_item:
            log("No random video found to play.")
            return

        # Clear the playlist and add first item
        xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0",
            "method": "Playlist.Clear",
            "params": {"playlistid": 1},
            "id": "playlist_clear"
        }))

        xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0",
            "method": "Playlist.Add",
            "params": {"playlistid": 1, "item": first_item},
            "id": "playlist_add_first"
        }))

        xbmc.sleep(100)

        xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0",
            "method": "Player.Open",
            "params": {"item": {"playlistid": 1, "position": 0}},
            "id": "player_open"
        }))
        
        xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
        
        # Determine remaining items
        remaining_items = []

        if first_item and 'episodeid' in first_item[0]:
            # Fetch TV show ID from first episode
            episode_id = first_item[0]['episodeid']
            episode_info = json.loads(xbmc.executeJSONRPC(json.dumps({
                "jsonrpc": "2.0",
                "method": "VideoLibrary.GetEpisodeDetails",
                "params": {"episodeid": episode_id, "properties": ["tvshowid"]},
                "id": "get_episode"
            })))['result']['episodedetails']
            tvshow_id = episode_info['tvshowid']

            # Get remaining episodes from the same TV show
            remaining_items = self.get_random_library_video(first_only=False, tvshow_id=tvshow_id)
        else:
            # For movies/musicvideos, get random items
            remaining_items = self.get_random_library_video(first_only=False)

        # Add remaining items to playlist
        for item in remaining_items:
            xbmc.executeJSONRPC(json.dumps({
                "jsonrpc": "2.0",
                "method": "Playlist.Add",
                "params": {"playlistid": 1, "item": item},
                "id": "playlist_add_rest"
            }))

        xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
        log("Random playlist started with first video playing immediately.")
