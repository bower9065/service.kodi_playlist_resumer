from .common import *
from .store import Store
import xbmc
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

    resumed_playback = Store.kodi_player.resume_if_was_playing()
    if not resumed_playback and not Store.kodi_player.isPlayingVideo():
        Store.kodi_player.autoplay_random_if_enabled()

    while not Store.kodi_event_monitor.abortRequested():
        if Store.kodi_event_monitor.waitForAbort(1):
            break

    footprints(False)