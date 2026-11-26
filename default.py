# -*- coding: utf-8 -*-
import sys

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action in ("rebuild_library_cache", "rebuild_ignored_cache"):
        import xbmcgui
        try:
            if action == "rebuild_library_cache":
                from resources.lib import library_cache
                if not library_cache.request_library_rebuild():
                    raise RuntimeError("Could not request library rebuild")
                message = "Library cache rebuild requested"
            else:
                from resources.lib import context
                rows = context.collect_ignored_ids(force=True)
                if context.CONTEXT_MONITOR.abortRequested():
                    raise RuntimeError("Rebuild interrupted")
                message = "Smart-playlist cache rebuilt: {} items".format(len(rows))
            xbmcgui.Dialog().notification("Playlist Resumer", message)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            import xbmc
            xbmc.log('###Kodi Playlist Resumer - ' + ("Play Random cache rebuild: " + str(error)), xbmc.LOGERROR)
            xbmcgui.Dialog().notification("Playlist Resumer", str(error), xbmcgui.NOTIFICATION_WARNING)
    else:
        from resources.lib import playback_resumer
        playback_resumer.run()
