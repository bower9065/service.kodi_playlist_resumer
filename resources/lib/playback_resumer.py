from .common import *
from .store import Store
import xbmc
import time
from .monitor import KodiEventMonitor
from .player import KodiPlayer
from . import library_cache

def run():
    """
    This is 'main'

    :return:
    """
    idle_started = (time.monotonic(), xbmc.getGlobalIdleTime())
    footprints()
    config = Store()
    Store.kodi_event_monitor = KodiEventMonitor()
    Store.kodi_player = KodiPlayer(xbmc.Player)
    cache_worker = library_cache.BackgroundCacheWorker(Store.kodi_event_monitor)
    Store.kodi_event_monitor.library_cache_worker = cache_worker
    # The worker is attached now to record any library changes, but do not
    # launch expensive cache queries until Kodi has finished startup restore.

    if Store.kodi_event_monitor.abortRequested():
        footprints(False)
        return
    Store.kodi_player.startup_playback(idle_started=idle_started)

    # The cache is an optimization: startup resume and Auto Play Random take
    # priority over its initial database queries.
    if not Store.kodi_event_monitor.abortRequested():
        cache_worker.start()

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
   