from .common import *
from . import video_queue
import os
import json
import time
import tempfile
import threading
import xml.etree.ElementTree as ElementTree
import xbmc

class Store:
    """
    Helper class to read in and store the addon settings, and to provide a centralised store
    """
    save_interval_seconds = 15
    ignore_seconds_at_start = 180
    ignore_percent_at_end = 8
    resume_on_startup = False
    restore_priority = "video"
    state_file = ""
    _state_lock = threading.RLock()
    autoplay_random = True
    autoplay_queue_completion = True
    queue_completion_delay = 12
    kodi_event_monitor = None
    player_monitor = None
    just_woke = False
    just_suspend = False
    currently_playing_file_path = ''
    type_of_video = None
    was_trailer = False
    was_audio = False
    library_id = -1
    paused_time = None
    length_of_currently_playing_file = 0
    video_types_in_library = {'episodes': True, 'movies': True, 'musicvideos': True}
    
    def __init__(self):
        """
        Load in the addon settings and do some basic initialisation stuff
        """
        Store.load_config_from_settings()
        if not os.path.exists(PROFILE):
            os.makedirs(PROFILE)
        Store.state_file = os.path.join(PROFILE, "resume data.json")
        advancedsettings_file = xbmcvfs.translatePath("special://profile/advancedsettings.xml")
        root = None
        try:
            root = ElementTree.parse(advancedsettings_file).getroot()
            log("Found and parsed advancedsettings.xml")
        except (ElementTree.ParseError, IOError):
            log("Could not find/parse advancedsettings.xml, will use defaults")
        if root is not None:
            element = root.find('./video/ignoresecondsatstart')
            if element is not None:
                log("Found advanced setting ignoresecondsatstart")
                Store.ignore_seconds_at_start = int(element.text)
            element = root.find('./video/ignorepercentatend')
            if element is not None:
                log("Found advanced setting ignorepercentatend")
                Store.ignore_percent_at_end = int(element.text)
        log(f"Using ignoresecondsatstart: {Store.ignore_seconds_at_start}, ignorepercentatend: {Store.ignore_percent_at_end}")

    @staticmethod
    def clear_old_play_details(keep_resume_snapshot=False):
        """
        As soon as a new file is played, clear out all old references to anything that was being stored as the currently playing file
        :return:
        """
        log("New playback - clearing legacy now playing details")
        Store.library_id = None
        Store.currently_playing_file_path = None
        Store.type_of_video = None
        Store.paused_time = None
        Store.length_of_currently_playing_file = None
        if not keep_resume_snapshot:
            Store.update_state('video', {})

    @staticmethod
    def load_config_from_settings():
        """
        Load in the addon settings, at start or reload them if they have been changed
        :return:
        """
        log("Loading configuration")
        Store.save_interval_seconds = int(float(ADDON.getSetting("saveintervalsecs")))
        Store.randomitems = int(get_setting("randomitems"))
        Store.resume_on_startup = get_setting_as_bool("resumeonstartup")
        Store.restore_priority = get_setting("restorepriority").lower() or "video"
        if Store.restore_priority not in ("video", "music", "last_used"):
            Store.restore_priority = "video"
        Store.autoplay_random = get_setting_as_bool("autoplayrandom")   
        Store.autoplay_queue_completion = get_setting_as_bool('autoplay_queue_completion')
        Store.queue_completion_delay = get_setting('queue_completion_delay')
        Store.resume_if_stopped = get_setting_as_bool("resumeifstopped")
        Store.resume_delay = get_setting('resume_delay')
        Store.resume_offset = get_setting('resumeoffset')      
        Store.log_configuration()

    @staticmethod
    def log_configuration():
        log(f'Will save a resume point every {Store.save_interval_seconds} seconds')
        log(f'Resume on startup: {Store.resume_on_startup}')
        log(f'Restore priority: {Store.restore_priority}')
        log(f'Autoplay random video: {Store.autoplay_random}')

    @staticmethod
    def _read_state_file(path):
        """Read a valid state file; callers decide when to try the backup."""
        with open(path, 'r', encoding='utf-8') as handle:
            state = json.load(handle)
        if not isinstance(state, dict):
            raise ValueError('state root is not a JSON object')
        empty = {'version': 1, 'video': {}, 'music': None, 'last_used': None}
        if not isinstance(state.get('video'), dict):
            state['video'] = {}
        return {**empty, **state}

    @staticmethod
    def read_state():
        """Read the unified resume state, falling back to its last good backup."""
        empty = {'version': 1, 'video': {}, 'music': None, 'last_used': None}
        with Store._state_lock:
            if not Store.state_file:
                return empty
            for candidate in (Store.state_file, Store.state_file + '.bak'):
                try:
                    state = Store._read_state_file(candidate)
                    if candidate.endswith('.bak'):
                        log('Recovered resume data from resume data.json.bak')
                    return state
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, TypeError) as error:
                    log('Could not read %s: %s' % (os.path.basename(candidate), error))
            return empty

    @staticmethod
    def _write_state_file(destination, state):
        """Commit indented JSON on the same filesystem with atomic replacement."""
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode='w', encoding='utf-8', delete=False,
                    dir=os.path.dirname(destination),
                    prefix='.resume-data-', suffix='.tmp') as handle:
                temporary = handle.name
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass

    @staticmethod
    def update_state(section, value, last_media=None):
        """Update one section atomically, backing up the previous good state.

        Do not back up an unreadable primary; this preserves the last good
        backup until a repaired primary is successfully written.
        """
        if section not in ('video', 'music', 'last_used') or not Store.state_file:
            return False
        with Store._state_lock:
            destination = Store.state_file
            try:
                previous = Store._read_state_file(destination)
            except (FileNotFoundError, OSError, ValueError, TypeError):
                previous = None
            state = (previous.copy() if previous is not None else Store.read_state())
            marker = state.get('last_used')
            media_changed = last_media and (not isinstance(marker, dict) or marker.get('media') != last_media)
            if state.get(section) == value and not media_changed:
                return False
            if previous is not None:
                try:
                    Store._write_state_file(destination + '.bak', previous)
                except OSError as error:
                    log('Could not back up resume data.json: %s' % error)
            state[section] = value
            if media_changed:
                state["last_used"] = {"media": last_media, "timestamp": time.time()}
            try:
                Store._write_state_file(destination, state)
                return True
            except (OSError, TypeError, ValueError) as error:
                log('Could not update resume data.json (%s): %s' % (section, error))
                return False

    @staticmethod
    def has_saved_resume_snapshot():
        """Whether a viable saved queue exists that startup should preserve.

        If restoration fails (for example a missing media path), Auto Play
        Random must not overwrite that saved queue before the user can retry.
        """
        state = Store.read_state()
        for kind in ('video', 'music'):
            snapshot = state.get(kind)
            if (isinstance(snapshot, dict) and snapshot.get('items')
                    and snapshot.get('position') is not None
                    and snapshot.get('seconds') is not None):
                return True
        return False

    @staticmethod
    def get_video_snapshot():
        return Store.read_state()['video']

    @staticmethod
    def update_video_snapshot(**fields):
        with Store._state_lock:
            video = Store.read_state()['video'].copy()
            # updated_at is metadata: do not change it (or rewrite the large
            # playlist) when the actual saved fields have not changed.
            if all(video.get(key) == value for key, value in fields.items()):
                return
            video.update(fields)
            video['updated_at'] = time.time()
            Store.update_state('video', video)

    @staticmethod
    def get_music_snapshot():
        return Store.read_state().get('music')

    @staticmethod
    def save_music_snapshot(snapshot):
        with Store._state_lock:
            previous = Store.get_music_snapshot()
            if isinstance(snapshot, dict) and isinstance(previous, dict):
                new_core = {k: v for k, v in snapshot.items() if k not in ('seconds', 'updated_at')}
                old_core = {k: v for k, v in previous.items() if k not in ('seconds', 'updated_at')}
                if new_core == old_core and previous.get('seconds') == 0:
                    return
            Store.update_state('music', snapshot, last_media='music')

    @staticmethod
    def clear_music_snapshot():
        Store.update_state('music', None)

    @staticmethod
    def record_last_used_media(media):
        if media not in ('music', 'video'):
            return
        with Store._state_lock:
            marker = Store.read_state().get('last_used')
            if isinstance(marker, dict) and marker.get('media') == media:
                return
            Store.update_state('last_used', {'media': media, 'timestamp': time.time()})
        log(f'Last used playback type: {media}')

    @staticmethod
    def get_last_used_media():
        """Choose last played type; on fresh installs use newest snapshot."""
        state = Store.read_state()
        marker = state.get('last_used')
        if isinstance(marker, dict) and marker.get('media') in ('music', 'video'):
            return marker['media']
        candidates = []
        for media in ('music', 'video'):
            snapshot = state.get(media)
            if isinstance(snapshot, dict) and snapshot:
                timestamp = snapshot.get('updated_at', 0)
                if not isinstance(timestamp, (int, float)):
                    timestamp = 0
                candidates.append((timestamp, media))
        return max(candidates)[1] if candidates else 'video'

    @staticmethod
    def is_excluded(full_path):
        """
        Check exclusion settings for a given file
        :param full_path: the full path of the file to check if is excluded
        :return:
        """
        if not full_path:
            return True
        log(f'Store.isExcluded(): Checking exclusion settings for [{full_path}]')
        if (full_path.find("pvr://") > -1) and getSettingAsBool('ExcludeLiveTV'):
            log('Store.isExcluded(): Video is PVR (Live TV), which is currently set as an excluded source.')
            return True
        if (full_path.find("http://") > -1 or full_path.find("https://") > -1) and get_setting_as_bool('ExcludeHTTP'):
            log("Store.isExcluded(): Video is from an HTTP/S source, which is currently set as an excluded source.")
            return True
        exclude_path = get_setting('exclude_path')
        if exclude_path and get_setting_as_bool('ExcludePathOption'):
            if full_path.find(exclude_path) > -1:
                log(f'Store.isExcluded(): Video is playing from [{exclude_path}], which is set as excluded path 1.')
                return True
        exclude_path2 = get_setting('exclude_path2')
        if exclude_path2 and get_setting_as_bool('ExcludePathOption2'):
            if full_path.find(exclude_path2) > -1:
                log(f'Store.isExcluded(): Video is playing from [{exclude_path2}], which is set as excluded path 2.')
                return True
        exclude_path3 = get_setting('exclude_path3')
        if exclude_path3 and get_setting_as_bool('ExcludePathOption3'):
            if full_path.find(exclude_path3) > -1:
                log(f'Store.isExcluded(): Video is playing from [{exclude_path3}], which is set as excluded path 3.')
                return True
        return False

    @staticmethod
    def update_current_playing_file_path(filepath):
        """
        Persistently tracks the currently playing file (in case of crash, for possible resuming)
        """
        if Store.is_excluded(filepath):
            log("Skipping excluded filepath: " + filepath)
            Store.clear_old_play_details()
            return
        query = {
            "jsonrpc": "2.0",
            "method": "Files.GetFileDetails",
            "params": {
                "file": filepath,
                "media": "video",
                "properties": []
            },
            "id": "fileDetailsCheck"
        }
        log(f'Executing JSON-RPC: {json.dumps(query)}')
        json_response = json.loads(xbmc.executeJSONRPC(json.dumps(query)))
        log(f'JSON-RPC Files.GetFileDetails response: {json.dumps(json_response)}')
        try:
            Store.type_of_video = json_response['result']['filedetails']['type']
        except KeyError:
            Store.library_id = -1
            log(f"ERROR: Kodi did not return even an 'unknown' file type for: {filepath}")
            Store.type_of_video = None
        if Store.type_of_video in ['episode', 'movie', 'musicvideo']:
            Store.library_id = json_response['result']['filedetails']['id']
        else:
            Store.library_id = None
        log(f'Kodi type: {Store.type_of_video}, library id: {Store.library_id}')
        if Store.type_of_video in ['episode', 'movie', 'musicvideo']:
            # A new video may have replaced an abandoned fast-start queue.
            # Passing its actual ID lets the guard release for unrelated items.
            current_identity = {Store.type_of_video + 'id': Store.library_id}
            if video_queue.snapshots_blocked(current_identity):
                log("Video queue is building; delaying snapshot write")
                Store.currently_playing_file_path = None
                return
            player_query = {
                "jsonrpc": "2.0",
                "method": "Player.GetProperties",
                "params": {
                    "playerid": 1,
                    "properties": ["position"]
                },
                "id": "player_properties"
            }
            player_props = json.loads(xbmc.executeJSONRPC(json.dumps(player_query)))
            live_items = video_queue._live_queue_items()
            if live_items is None:
                log("Could not read complete video playlist; retaining saved snapshot")
                Store.currently_playing_file_path = None
                return
            # The player's numeric position is not a reliable identity: it
            # can become stale when playback switches tracks or shuffle changes.
            # Use the actual file's Kodi library ID to find its queue entry.
            try:
                reported_position = int(player_props['result']['position'])
            except (KeyError, ValueError, TypeError):
                reported_position = -1
            filtered = [{item['type'] + 'id': item['id']} for item in live_items
                        if item.get('type') in ('movie', 'episode', 'musicvideo') and type(item.get('id')) is int]
            if len(filtered) != len(live_items):
                log('Video queue contains unrecognized entries; refusing partial snapshot')
                Store.currently_playing_file_path = None
                return

            current_item = {Store.type_of_video + 'id': Store.library_id}
            matches = [i for i, item in enumerate(filtered) if item == current_item]
            if not matches:
                log(f"Refusing to save a mismatched resume queue: active={current_item}, "
                    f"reported_position={reported_position}, saved_queue={len(filtered)}")
                # Keep the last valid persisted snapshot until Kodi has a
                # consistent queue; do not replace it with the wrong video.
                Store.currently_playing_file_path = None
                return

            final_position = (reported_position if reported_position in matches
                              else matches[0])
            if final_position != reported_position:
                log(f"Corrected stored playlist position from {reported_position} "
                    f"to {final_position}, using currently playing ID {current_item}")

            if video_queue.snapshots_blocked(current_item):
                log("Video queue still building; keeping previous resume snapshot")
                return
            Store.update_video_snapshot(
                items=filtered,
                position=final_position,
                resume_item={'item': current_item, 'file': filepath})
            Store.currently_playing_file_path = filepath
            log(f'Last played library file set to: {filepath}')
        else:
            Store.currently_playing_file_path = filepath
            Store.update_video_snapshot(items=filepath, position=0, resume_item=None)
            log(f'Last played file set to: {filepath}')
