from random import randint

from .common import *
from .store import Store
import json
import time
import os
import xbmc


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
#        xbmc.log('-------------------------------------------------------------ended--------------- )', xbmc.LOGINFO)
        log("onPlayBackEnded")
        self.update_resume_point(-1)
        # fix for Kodi wasnt playing next items in playlist after being shutdown by Android TV    
        xbmc.sleep(int(Store.idle_delay) * 1000)
        exit_loop = False
        while not self.isPlaying() and not Store.kodi_event_monitor.abortRequested() and exit_loop == False:
            for i in range(0, 300): 
                if xbmc.getGlobalIdleTime() >= int(Store.idle_delay): #xbmc.getCondVisibility("System.IdleTime('Store.idle_delay')"):    
                    self.autoplay_random_if_enabled()
                    exit_loop = True
                    break
                    #xbmc.sleep(1000)
                xbmc.sleep(1000)

    def onPlayBackStopped(self):  # user stopped / shutdown
#        xbmc.log('------------------------------------------------------------stopped--------------- )', xbmc.LOGINFO)
        log("onPlayBackStopped")
        self.update_resume_point(-2)

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

    def onAVStarted(self):
        log("onAVStarted")

        # Clean up - get rid of any data about any files previously played
        Store.clear_old_play_details()

        if not self.isPlayingVideo():
            log("Not playing a video - skipping: " + self.getPlayingFile())
            return

        xbmc.sleep(1500)  # give it a bit to start playing and let the stopped method finish
        Store.update_current_playing_file_path(self.getPlayingFile())
        Store.length_of_currently_playing_file = self.getTotalTime()
        try:
            self.update_resume_point(self.getTime())
        except RuntimeError:
            log('Could not get current playback time from player')
        while self.isPlaying() and not Store.kodi_event_monitor.abortRequested():

            try:
                self.update_resume_point(self.getTime())
            except RuntimeError:
                log('Could not get current playback time from player')

            for i in range(0, Store.save_interval_seconds):
                # Shutting down or not playing video anymore...stop handling playback
                if Store.kodi_event_monitor.abortRequested() or not self.isPlaying():
                    return
                # Otherwise sleep 1 second & loop
                xbmc.sleep(1000)

    def update_resume_point(self, seconds):
        """
        This is where the work is done - stores a new resume point in the Kodi library for the currently playing file

        :param: seconds: the time to update the resume point to.  @todo add notes on -1, -2 etc here!
        :param: Store.library_id: the Kodi library id of the currently playing file
        :return: None
        """

        # cast to int just to be sure
        seconds = int(seconds)
        
        # short circuit if we haven't got a record of the file that is currently playing
        if not Store.currently_playing_file_path:
            log("No valid currently_playing_file_path found - therefore not setting resume point")
            return

        # short circuit if weird library ID
        if Store.library_id and Store.library_id < 0:
            log(f"No/invalid library id ({Store.library_id}) for {Store.currently_playing_file_path}")
            return

        # wait to see if video stopped for kodi shutdown or was stopped by user
        if seconds == -2:
            # check if Kodi is actually shutting down (abortRequested happens slightly after onPlayBackStopped, hence the sleep/wait/check)
            for i in range(0, 100):
                if Store.kodi_event_monitor.abortRequested():
                    log("Kodi is shutting down, and will save resume point")
                    # Kodi is shutting down while playing a video.
                    seconds = int(self.getTime())                   
                    break

                if self.isPlaying():
                    # a new video has started playing. Kodi is not actually shutting down                   
                    return
                xbmc.sleep(100) 

        # at this point user stopped video, clear our resume point (Kodi should save its resume point on its own)
        if seconds == -2:
            log("Video was stopped by user, and will remove our resume point")                  
            if Store.resume_if_stopped:
                seconds = int(self.getTime())
            else:
                Store.clear_old_play_details()
            return
            
        # file ended normally, reset kodi's resume point and save our resume point
        if seconds == -1:
            log("Removing resume points because the file has ended normally")
#            if Store.resume_if_stopped:
#                seconds = 0
#            else:
            Store.clear_old_play_details()
            return 
            #seconds = 0
            # fix for kodi was overwriting native resume point with old reume point after video ended, causing a loop
#            xbmc.sleep(3000)
            
        # if current time < Kodi's ignoresecondsatstart setting, save as 0 seconds
        if 0 < seconds < Store.ignore_seconds_at_start:
            log(f'Resume point ({seconds}) is below Kodi\'s ignoresecondsatstart'
                f' setting of {Store.ignore_seconds_at_start}')
            seconds = 0
            with open(Store.file_to_store_resume_point, 'w') as f:
                f.write(str(seconds))

        # if current time > Kodi's ignorepercentatend setting, save resume point
        percent_played = int((seconds * 100) / Store.length_of_currently_playing_file)
        if percent_played > (100 - Store.ignore_percent_at_end):
            log(f'Resume point as current percent played ({percent_played}) is above Kodi\'s ignorepercentatend'
                f' setting of {Store.ignore_percent_at_end}')
            seconds = 0
            with open(Store.file_to_store_resume_point, 'w') as f:
                f.write(str(seconds))

        # if seconds between start ignore and end ignore, save resume point
        if percent_played < (100 - Store.ignore_percent_at_end) and seconds > Store.ignore_seconds_at_start :
            with open(Store.file_to_store_resume_point, 'w') as f:
                f.write(str(seconds))                             

        # First update the resume point in the tracker file for later retrieval if needed
        log(f'Setting custom resume seconds to {seconds}')

        # Log what we are doing
        if seconds == 0:
            log(f'Removing resume point for: {Store.currently_playing_file_path}, type: {Store.type_of_video}, library id: {Store.library_id}')
        else:
            log(f'Setting resume point for: {Store.currently_playing_file_path}, type: {Store.type_of_video}, library id: {Store.library_id}, to: {seconds} seconds')

        # Determine the JSON-RPC setFooDetails method to use and what the library id name is based of the type of video
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

        # For debugging - let's retrieve and log the current resume point to check it was actually set as intended...
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
        # adjustable playback start delay        
        xbmc.sleep(int(Store.playback_delay) * 1000)
        exit_loop = False
        while not self.isPlaying() and not Store.kodi_event_monitor.abortRequested() and exit_loop == False:
            for i in range(0, 300): 
                if xbmc.getGlobalIdleTime() >= int(Store.idle_delay):                             
                    if Store.resume_on_startup \
                            and os.path.exists(Store.file_to_store_resume_point) \
                            and os.path.exists(Store.file_to_store_playlist_items) \
                            and not self.isPlayingVideo() and not Store.kodi_event_monitor.abortRequested():
                        # check how many items are in playlist
                        with open(Store.file_to_store_playlist_items, 'r',) as f:
                            items = f.read()
                        with open(Store.file_to_store_resume_point, 'r') as f:
                            try:
                                resume_point = float(f.read())
                            except Exception as e:
                                log("Error reading resume point from file, therefore not resuming.")
                                return
                        # if playlist has no library items use filepath method                
                        if "movieid" not in items \
                            and "episodeid" not in items\
                            and "musicvideoid" not in items:
                            # neg 1 means the video wasn't playing when Kodi ended
                            if resume_point == '':
                                log("Not resuming playback because nothing was playing when Kodi last closed")
                                return False

                            with open(Store.file_to_store_playlist_items, 'r') as f:
                                full_path = f.read()

                            str_timestamp = '%d:%02d' % (resume_point / 60, resume_point % 60)
                            log(f'Will resume playback at {str_timestamp} of {full_path}')

                            self.play(full_path)
                        # if playlist has library items use playlist method                 
                        else:
                            with open(Store.file_to_store_playlist_shuffled, 'r',) as f:
                                shuffled = f.read()
                            with open(Store.file_to_store_playlist_items, 'r',) as f:
                                items = f.read()
                            # if playlist is shuffled turn off shuffle until playlist is added
                            if shuffled == "True":
                                xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Player.SetShuffle","params":{"playerid":1,"shuffle":false},"id":"player_shuffle"}')
                            with open(Store.file_to_store_playlist_position, 'r',) as f:
                                position = f.read()            
                            xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Playlist.Add","params":{"item":' + items + ',"playlistid":1},"id":"playlist_add"}')
                            xbmc.sleep(100)       
                            xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Player.Open","params":{"item":{"playlistid":1,"position":' + position + '}},"id":"player_open"}')
                            # if playlist was shuffled turn on shuffle after playlist is added
                            if shuffled == "True":
                                xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Player.SetShuffle","params":{"playerid":1,"shuffle":true},"id":"player_shuffle"}') 
                            str_timestamp = '%d:%02d' % (resume_point / 60, resume_point % 60)
                            log(f'Will resume playback at {str_timestamp} of playlist')

                        # wait up to 10 secs for the video to start playing before we try to seek
                        for i in range(0, 1000):
                            if not self.isPlayingVideo() and not Store.kodi_event_monitor.abortRequested():
                                xbmc.sleep(100)
                            else:
                                notify(f'Resuming playback at {str_timestamp}', xbmcgui.NOTIFICATION_INFO)
                                # adjustable resume point offset
                                offset = resume_point - int(Store.resume_offset)
                                if offset > 0:
                                    self.seekTime(offset)
                                    return True
                                else:
                                    self.seekTime(resume_point)
                                    return True                    
                    exit_loop = True #return False
                    break
                xbmc.sleep(1000)
    def get_random_library_video(self):
        """
        Get a random video from the library for playback

        :return:
        """

        # Short circuit if library is empty
        if not Store.video_types_in_library['episodes'] \
                and not Store.video_types_in_library['movies'] \
                and not Store.video_types_in_library['musicvideos']:
            log('No episodes, movies, or music videos exist in the Kodi library. Cannot autoplay a random video.')
            return

        random_int = randint(0, 2)
        if random_int == 0:
            if get_setting_as_bool("randomtv"):
                result_type = 'episodes'
                method = "GetEpisodes"
            else:
                return self.get_random_library_video()
        elif random_int == 1:
            if get_setting_as_bool("randommovies"):
                result_type = 'movies'
                method = "GetMovies"
            else:
                return self.get_random_library_video()            
        elif random_int == 2:
            if get_setting_as_bool("randommusicvideos"):
                result_type = 'musicvideos'
                method = "GetMusicVideos"
            else:
                return self.get_random_library_video() 
        # if the randomly chosen type is not in the library, keep randomly trying until we get
        # a type that is in the library...
        if not Store.video_types_in_library[result_type]:
            return self.get_random_library_video()  # get a different one

        log(f'Getting a random video from: {result_type}')

        query = {
            "jsonrpc": "2.0",
            "id": "randomLibraryVideo",
            "method": "VideoLibrary." + method,
            "params": {
                "limits": {
                    "end": 1
                },
                "sort": {
                    "method": "random"
                },
                "properties": [
                    "file"
                ]
            }
        }

        log(f'Executing JSON-RPC: {json.dumps(query)}')
        json_response = json.loads(xbmc.executeJSONRPC(json.dumps(query)))
        log(f'JSON-RPC VideoLibrary.{method} response: {json.dumps(json_response)}')

        # found a video!
        if json_response['result']['limits']['total'] > 0:
            Store.video_types_in_library[result_type] = True
            return json_response['result'][result_type][0]['file']
        # no videos of this type
        else:
            log("There are no " + result_type + " in the library")
            Store.video_types_in_library[result_type] = False
            return self.get_random_library_video()

    def autoplay_random_if_enabled(self):
        """
        Play a random video, if the setting is enabled
        :return:
        """

        if Store.autoplay_random:

            log("Autoplay random is enabled in addon settings, so will play a new random video now.")

            video_playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)

            # make sure the current playlist has finished completely
            if not self.isPlayingVideo() \
                    and (video_playlist.getposition() == -1 or video_playlist.getposition() == video_playlist.size()):
                full_path = self.get_random_library_video()
                log("Auto-playing next random video because nothing is playing and playlist is empty: " + full_path)
                self.play(full_path)
                notify(f'Auto-playing random video: {full_path}', xbmcgui.NOTIFICATION_INFO)
            else:
                log(f'Not auto-playing random as playlist not empty or something is playing.')
                log(f'Current playlist position: {video_playlist.getposition()}, playlist size: {video_playlist.size()}')

