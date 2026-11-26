import xbmc
from .common import *
from .store import Store
from . import video_queue
import time

class KodiEventMonitor(xbmc.Monitor):

    def __init__(self, *args, **kwargs):
        xbmc.Monitor.__init__(self)
        self.library_cache_worker = None
        log('KodiEventMonitor __init__')
        
    def onSettingsChanged(self):
        if self.abortRequested():
            return
        log('onSettingsChanged - reload them.')
        Store.load_config_from_settings()

    def onAbortRequested(self):
        xbmc.log('###Kodi Playlist Resumer - ' + ('----(Playlist Resumer)...ABORT detected.'), xbmc.LOGINFO)
        log("Abort Requested")
        if Store.kodi_player is not None:
            Store.kodi_player.mark_shutdown("abort requested")

    def onNotification(self, sender, method, data):
        if method == "System.OnQuit":
            if Store.kodi_player is not None:
                Store.kodi_player.mark_shutdown(method)
            return
        if self.abortRequested():
            return
        if method in ("Playlist.OnAdd", "Playlist.OnRemove", "Playlist.OnClear", "Playlist.OnUpdate", "Playlist.OnSwap", "Player.OnPropertyChanged"):
            try:
                import json
                payload = json.loads(data)
                if payload.get("playlistid") == 1 or (method == "Player.OnPropertyChanged" and payload.get("player", {}).get("playerid") == 1 and "shuffled" in payload.get("property", {})):
                    Store.video_queue_generation += 1
                if payload.get("playlistid") == 0 and Store.kodi_player is not None:
                    player = Store.kodi_player
                    player._music_queue_generation = getattr(player, '_music_queue_generation', 0) + 1
            except (ValueError, TypeError, AttributeError):
                pass
        if self.library_cache_worker is not None:
            self.library_cache_worker.on_notification(method, data)
        if method in ("System.OnSleep", "System.OnSuspend"):
            log("Suspend detected")
            Store.just_suspend = True
            if Store.kodi_player is not None:
                Store.kodi_player.cancel_pending_stop(method)
                log("KPR STOP: sleep detected - preserving resume snapshot", level=xbmc.LOGINFO)
        elif method in ("System.OnWake", "System.OnResume"):
            if video_queue.snapshots_blocked():
                log("Ignoring wake playlist reset during active XSP queue construction")
                return
            idle_started = (time.monotonic(), xbmc.getGlobalIdleTime())
            Store.just_woke = True
            Store.kodi_player.cancel_pending_stop(method)
            Store.kodi_player.stop()
            xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Playlist.Clear","params":{"playlistid":1},"id":"playlist_clear"}')
            log("Wake detected")
            Store.just_woke = True
            if self.abortRequested():
                return
            Store.kodi_player.startup_playback(idle_started=idle_started)
