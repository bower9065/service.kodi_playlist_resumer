"""Keep current add-on data and remove obsolete or foreign profile files."""
import os
import re
import time
import xbmc

KEEP = {'settings.xml', 'resume data.json', 'resume data.json.bak',
        'play_random_library_cache.json', 'play_random_ignored_cache.json',
        'play_random_ignored_cache.json.invalidated'}
TEMP = re.compile(r'(?:kpr_internal_[0-9a-f]{32}_\d+\.xsp|music-resume\.m3u8(?:\.tmp)?|\.resume-data-.*\.tmp|video playlist loading\.json(?:\.[0-9a-f]{32}\.tmp)?|play_random_(?:library_cache|ignored_cache)\.json\.tmp)\Z')
LOCKS = {'play_random_library_cache.json.lock', 'play_random_ignored_cache.json.lock'}


def clean_profile(profile):
    if not profile or not os.path.isdir(profile) or os.path.islink(profile):
        return
    def warn(error):
        xbmc.log('###Kodi Playlist Resumer - Startup cleanup: ' + str(error), xbmc.LOGWARNING)
    removed = 0
    try:
        entries = list(os.scandir(profile))
    except OSError as error:
        warn(error)
        return
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_file(follow_symlinks=False):
                if entry.name in KEEP:
                    continue
                if TEMP.fullmatch(entry.name) and time.time() - entry.stat(follow_symlinks=False).st_mtime < 300:
                    continue
                os.remove(entry.path)
                removed += 1
            elif entry.is_dir(follow_symlinks=False) and entry.name in LOCKS:
                if time.time() - entry.stat(follow_symlinks=False).st_mtime >= 300:
                    os.rmdir(entry.path)
                    removed += 1
        except OSError as error:
            warn(error)
    if removed:
        xbmc.log('###Kodi Playlist Resumer - Startup cleanup removed %d files or stale artifacts' % removed, xbmc.LOGINFO)
