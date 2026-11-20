from .common import *
from .store import Store
import xbmc
import time
from .monitor import KodiEventMonitor
from .player import KodiPlayer

def run():
    """
    This is 'main'

    :return:
    """
    footprints()
    config = Store()
    Store.kodi_event_monitor = KodiEventMonitor()
    Store.kodi_player = KodiPlayer(xbmc.Player)

    if Store.kodi_event_monitor.abortRequested():
        footprints(False)
        return
    resumed_playback = Store.kodi_player.resume_if_was_playing()
    
    if Store.was_trailer:
        log("A trailer just finished playing. Blocking autoplay_random and resetting state.")
        Store.was_trailer = False
        resumed_playback = True
        
    if (resumed_playback == False and not Store.kodi_event_monitor.abortRequested()
            and not Store.kodi_player.isPlaying()):
        Store.kodi_player.autoplay_random_if_enabled()
        
    next_music_capture = 0
    while not Store.kodi_event_monitor.abortRequested():
        if Store.kodi_event_monitor.waitForAbort(1):
            break
        if Store.kodi_player.isPlayingAudio():
            if time.monotonic() >= next_music_capture:
                Store.kodi_player.capture_music_if_due()
                next_music_capture = time.monotonic() + Store.save_interval_seconds
        else:
            next_music_capture = 0

    footprints(False)
   