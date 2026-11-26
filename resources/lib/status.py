import xbmc
import xbmcgui
from xbmcaddon import Addon
from functools import wraps

addon = Addon()

class StatusProgress:
    """Text-only, nonmodal status overlay; no native percentage label."""
    def __init__(self, heading="Play Random", message="Preparing to play random videos...", lazy=False):
        self.dialog = None
        self.loading_succeeded = False
        if not lazy:
            self.update(0, heading, message)

    def _show(self, heading, message):
        try:
            self.dialog = xbmcgui.WindowXMLDialog(
                'KPRStatus.xml', addon.getAddonInfo('path'), 'Default', '720p')
            self.dialog.setProperty('KPRHeading', 'Playlist Resumer')
            self.dialog.setProperty('KPRMessage', message)
            self.dialog.show()
        except Exception as error:
            xbmc.log('###Kodi Playlist Resumer - ' + ('KPR status creation failed: ' + str(error)), xbmc.LOGWARNING)

    def update(self, percent, heading, message):
        if self.dialog is None:
            self._show(heading, message)
            return
        if self.dialog is not None:
            try:
                self.dialog.setProperty('KPRHeading', 'Playlist Resumer')
                self.dialog.setProperty('KPRMessage', message)
            except Exception as error:
                xbmc.log('###Kodi Playlist Resumer - ' + ('KPR status update failed: ' + str(error)), xbmc.LOGWARNING)

    def close(self):
        if self.dialog is not None:
            try:
                self.dialog.close()
            except Exception as error:
                xbmc.log('###Kodi Playlist Resumer - ' + ('KPR status close failed: ' + str(error)), xbmc.LOGWARNING)
            finally:
                self.dialog = None


def with_status(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        status = StatusProgress(lazy=True)
        try:
            return func(*args, status=status, **kwargs)
        finally:
            status.close()
    return wrapped
