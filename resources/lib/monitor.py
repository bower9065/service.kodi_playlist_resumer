import xbmc
from .common import *
from .store import Store
import time

class KodiEventMonitor(xbmc.Monitor):

    def __init__(self, *args, **kwargs):
        xbmc.Monitor.__init__(self)
        log('KodiEventMonitor __init__')
        
    def onSettingsChanged(self):
        log('onSettingsChanged - reload them.')
        Store.load_config_from_settings()

    def onAbortRequested(self):
        xbmc.log('----(Playlist Resumer)...ABORT detected.', xbmc.LOGINFO)
        log("Abort Requested")

    def onNotification(self, sender, method, data):
        if method in ("System.OnSleep", "System.OnSuspend"):
            log("Suspend detected")
            Store.just_suspend = True
        elif method in ("System.OnWake", "System.OnResume"):
            Store.kodi_player.stop()
            xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Playlist.Clear","params":{"playlistid":1},"id":"playlist_clear"}')
            log("Wake detected")
            Store.just_woke = True
            resumed_playback = Store.kodi_player.resume_if_was_playing()
            if not resumed_playback:
                Store.kodi_player.autoplay_random_if_enabled()