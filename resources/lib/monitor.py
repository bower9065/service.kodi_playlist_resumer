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
            #xbmc.log('----(Playlist Resumer)...Suspend detected.', xbmc.LOGINFO)
            log("Suspend detected")
            Store.just_suspend = True
        elif method in ("System.OnWake", "System.OnResume"):
            Store.kodi_player.stop()
            #xbmc.log('----(Playlist Resumer)...Wake detected. Attempting resume.', xbmc.LOGINFO)
            log("Wake detected")
            Store.just_woke = True
            resumed_playback = Store.kodi_player.resume_if_was_playing()
            if not resumed_playback:
                Store.kodi_player.autoplay_random_if_enabled()