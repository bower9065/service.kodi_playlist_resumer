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

    def onPlayBackSeekChapter(self, chapter):
        log(f'onPlayBackSeekChapter chapter: {chapter}')
        try:
            self.update_resume_point(self.getTime())
        except RuntimeError:
            log("Could not get playing time - seeked past end?")
            self.update_resume_point(0)
            pass

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
            for i in range(0, 5):
                if Store.kodi_event_monitor.abortRequested():
                    log("Kodi is shutting down, and will save resume point")
                    seconds = int(self.getTime())
                    break
                if self.isPlaying():
                    log("Kodi is playing a video, not shutting down")
                    return
                xbmc.sleep(1000) 
        if seconds == -2:                          
            if Store.resume_if_stopped:
                log("Video was stopped by user with Resume if Stopped on, and will save our resume point")   
                seconds = int(self.getTime())
            else:
                log("Video was stopped by user with Resume if Stopped off, and will remove our resume point")                  
                Store.clear_old_play_details()
                xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Playlist.Clear","params":{"playlistid":1},"id":"playlist_clear"}')
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
        Automatically resume a video after a crash, if one was playing...
        :return:
        """
        xbmc.log('----(Playlist Resumer)...Resuming playback.', xbmc.LOGINFO)
        monitor = xbmc.Monitor()
        if Store.resume_on_startup \
                and os.path.exists(Store.file_to_store_resume_point) \
                and os.path.exists(Store.file_to_store_playlist_items) \
                and os.path.exists(Store.file_to_store_playlist_shuffled) \
                and os.path.exists(Store.file_to_store_playlist_position):
            with open(Store.file_to_store_playlist_items, 'r') as f:
                items = f.read()
            if items != '[]':
                if not monitor.waitForAbort(max((int(Store.resume_delay) - 4), 0)):
                    if xbmc.getGlobalIdleTime() >= (int(Store.resume_delay) - 4):
                        notify(f'Preparing to resume playback...', xbmcgui.NOTIFICATION_INFO)
                        log("Notification displayed")
                        monitor.waitForAbort(4)
                    else:
                        notify(f'Resume playback canceled', xbmcgui.NOTIFICATION_INFO)
                        log("Resume stopped, idle time failed")
                if xbmc.getGlobalIdleTime() >= int(Store.resume_delay):
                    notify(f'Resuming playback...', xbmcgui.NOTIFICATION_INFO)
                    log("Resume starting, idle time passed")
                    xbmc.executebuiltin('ActivateWindow(busydialognocancel)')
                    with open(Store.file_to_store_resume_point, 'r') as f:
                        try:
                            resume_point = float(f.read())
                        except Exception:
                            log("Error reading resume point from file, therefore not resuming.")
                            return
                    if "movieid" not in items and "episodeid" not in items and "musicvideoid" not in items:
                        full_path = items
                        log("Resuming as filepath method")
                        self.play(full_path)
                    else:
                        log("Resuming as playlist method")
                        with open(Store.file_to_store_playlist_shuffled, 'r') as f:
                            shuffled = f.read()
                        with open(Store.file_to_store_playlist_position, 'r') as f:
                            position = f.read()
                        if shuffled == "True":
                            log("Shuffle turned off")
                            xbmc.executeJSONRPC(
                                '{"jsonrpc":"2.0","method":"Player.SetShuffle","params":{"playerid":1,"shuffle":false},"id":"player_shuffle"}'
                            )
                        xbmc.executeJSONRPC(
                            '{"jsonrpc":"2.0","method":"Playlist.Add","params":{"item":' + items + ',"playlistid":1},"id":"playlist_add"}'
                        )
                        monitor.waitForAbort(1)
                        xbmc.executeJSONRPC(
                            '{"jsonrpc":"2.0","method":"Player.Open","params":{"item":{"playlistid":1,"position":' + position + '}},"id":"player_open"}'
                        )
                        if shuffled == "True":
                            log("Shuffle turned on")
                            xbmc.executeJSONRPC(
                                '{"jsonrpc":"2.0","method":"Player.SetShuffle","params":{"playerid":1,"shuffle":true},"id":"player_shuffle"}'
                            )
                    xbmc.executebuiltin('Dialog.Close(busydialognocancel)')                 
                    waited = 0
                    while (not self.isPlayingVideo() or self.getTotalTime() == 0) \
                            and not Store.kodi_event_monitor.abortRequested() \
                            and waited < 50:
                        monitor.waitForAbort(0.2)
                        waited += 1
                    offset = resume_point - int(Store.resume_offset)
                    if offset > 0:
                        self.seekTime(offset)
                        log("Seeking. Seek offset is > 0")
                    else:
                        self.seekTime(resume_point)
                        log("Seeking. Seek offset is <= 0")
                    return True
                else:
                    log("Resume stopped, idle time failed")
                    return True
            else:
                log("Resume stopped, playlist is empty")
                return False
        else:
            log("Resume stopped, missing files")
            return False

    def get_random_library_video(self):
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
            return
        result_type, method = enabled_types[randint(0, len(enabled_types)-1)]
        log(f'Getting a random video from: {result_type}')
        query = {
            "jsonrpc": "2.0",
            "id": "randomLibraryVideo",
            "method": f"VideoLibrary.{method}",
            "params": {
                "limits": {"end": 100},
                "sort": {"method": "random"},
                "properties": ["file"]
            }
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
            return self.get_random_library_video()
        return json.dumps(random_list)

    def autoplay_random_if_enabled(self):
        """
        Play a random video, if the setting is enabled
        :return:
        """
        if Store.autoplay_random:
            xbmc.log('----(Playlist Resumer)...Playing random videos.', xbmc.LOGINFO)
            xbmc.sleep((int(Store.random_delay) - 4) * 1000)
            if xbmc.getGlobalIdleTime() >= (int(Store.random_delay) - 4):
                notify(f'Preparing to play random videos...', xbmcgui.NOTIFICATION_INFO)
                xbmc.sleep(4000)
            if xbmc.getGlobalIdleTime() >= int(Store.random_delay):
                notify(f'Playing random videos...', xbmcgui.NOTIFICATION_INFO)
                log("Auto Play Random starting, idle time passed")
                video_playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
                if not self.isPlaying() \
                        and (video_playlist.getposition() == -1 or video_playlist.getposition() == video_playlist.size()):
                    full_path = self.get_random_library_video()
                    xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Playlist.Clear","params":{"playlistid":1},"id":"playlist_clear"}')
                    xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Playlist.Add","params":{"item":' + full_path + ',"playlistid":1},"id":"playlist_add"}')
                    xbmc.sleep(100)       
                    xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Player.Open","params":{"item":{"playlistid":1,"position":0}},"id":"player_open"}')
                    log("Auto-playing next random video because nothing is playing and playlist is empty: " + full_path)                
                else:
                    log(f'Auto Play Random stopped, playlist not empty or something is playing.')
                xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
            else:
                notify(f'Play random videos canceled', xbmcgui.NOTIFICATION_INFO)
                log("Auto Play Random stopped, idle time failed")
        else:
            log("Auto Play Random stopped,  turned off")