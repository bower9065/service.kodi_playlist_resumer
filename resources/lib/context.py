import sys
import xbmc
from urllib.parse import parse_qs
import json
from xbmcaddon import Addon
import os
import xbmcvfs

if __name__ == '__main__':
    path = sys.listitem.getPath()
    dbid = xbmc.getInfoLabel('ListItem.DBID()')
    tvshow_title = xbmc.getInfoLabel('ListItem.TVShowTitle()')    
    tvshow_dbid = xbmc.getInfoLabel('ListItem.TvShowDBID()')    
    tvshow_season = 'Season ' + xbmc.getInfoLabel('ListItem.Season()')
    db_type = xbmc.getInfoLabel('listitem.DBTYPE()')
    path_type, db_path = path.split('://', 1)
    db_path = db_path.split('?', 1)
    query = parse_qs(db_path[1]) if len(db_path) > 1 else "null"
    db_path = db_path[0].rstrip('/').split('/')
    list_item = '"' + db_type + 'id":' + dbid 
   
    xbmc.executebuiltin('ActivateWindow(busydialognocancel)')
  
#Video Library
    if path_type == 'videodb':
        video_list = []    
        # set library rules
        if str('query') != "Null" and 'xsp' in query and 'rules' in query['xsp'][0]:              
            xsp = str((json.loads(query['xsp'][0]))['rules'])
            xsp = ',"filter":' + xsp.replace("'", '"')         
            ignored_rules = ['dateadded', 'inprogress', 'numwatched'] 
            has_xsp = False if any(x in str(xsp) for x in ignored_rules) else True 
        else:
            has_xsp = False
        if has_xsp == False:
            xsp = ''
#Ignore smart playlist items 
    #Get list of playlist names       
        if Addon().getSettingBool("IgnoreSmartPlaylistItems") and os.path.exists(xbmcvfs.translatePath("special://userdata/library/video/playlists/")):
            playlists = []
            ignored_ids = []
            playlist_list = xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Files.GetDirectory","params":{"properties": ["title"],"directory":"library://video/playlists/"}, "id":"get_directory"}')
            playlist_list = json.loads(playlist_list)
            playlist_list = playlist_list['result']['files']
            for item in playlist_list:
                final_playlist_list = '"' + str(item["file"]) + '"'                
                playlists.append(final_playlist_list)         
    #For items in each playlist
            for item in playlists:
                playlist_items = xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Files.GetDirectory","params":{"properties": ["title"],"directory":' + item + '}, "id":"get_directory"}')
                playlist_items = json.loads(playlist_items)
                playlist_items = playlist_items['result']['files']  
                playlist_items = json.loads(json.dumps(playlist_items))
    #Append name for items in each playlist
                for x, i in enumerate(playlist_items):
                    itemid = playlist_items[x]['id']
                    itemtype = playlist_items[x]['type']
                    final_playlist_items = '"' + str(itemtype) + 'id":' + str(itemid)
                    ignored_ids.append(final_playlist_items)        
        else:
            ignored_ids = []
    #Movies            
        if db_type == 'movie':
            video_list = xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"VideoLibrary.GetMovies","params":{"limits":{"end":100},"sort":{"method":"random"},"properties":["file"]' + xsp + '},"id":"get_random_movie"}')
            if has_xsp == False:
                xbmc.log('----(Playlist Resumer)...Playing random movies, NO RULES', xbmc.LOGINFO)
            else:
                xbmc.log('----(Playlist Resumer)...Playing random movies, RULES APPLIED', xbmc.LOGINFO)
    #TVShows
        if db_type == 'tvshow':
            #InProgress/RecentlyAdded
            if len(db_path) <= 2:
                tvshow_id = db_path[1]
                video_list = xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"VideoLibrary.GetEpisodes","params":{"tvshowid":' + tvshow_id + ',"limits":{"end":100},"sort":{"method":"random"},"properties":["file"]' + xsp + '},"id":"get_random_tvshow"}')
                if has_xsp == False:
                    xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title}, NO RULES', xbmc.LOGINFO)
                else:
                    xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title}, RULES APPLIED', xbmc.LOGINFO)
            #Titles
            else:
                tvshow_id = db_path[2]
                video_list = xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"VideoLibrary.GetEpisodes","params":{"tvshowid":' + tvshow_id + ',"limits":{"end":100},"sort":{"method":"random"},"properties":["file"]' + xsp + '},"id":"get_random_tvshow"}')
                if has_xsp == False:
                    xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title}, NO RULES', xbmc.LOGINFO)
                else:
                    xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title}, RULES APPLIED', xbmc.LOGINFO)
    #Seasons
        if db_type == 'season':
            #InProgress/RecentlyAdded
            if len(db_path)  <= 3:
                tvshow_id = db_path[1]
                season_id = db_path[2]
                #Has valid season id
                if int(season_id) > -1:
                    video_list = xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"VideoLibrary.GetEpisodes","params":{"tvshowid":' + tvshow_id + ',"season":' + season_id + ',"limits":{"end":100},"sort":{"method":"random"},"properties":["file"]' + xsp + '},"id":"get_random_season"}')
                    if has_xsp == False:
                        xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title} 'f'{tvshow_season}, NO RULES', xbmc.LOGINFO)
                    else:
                        xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title} 'f'{tvshow_season}, RULES APPLIED', xbmc.LOGINFO)
                else: 
                    video_list = xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"VideoLibrary.GetEpisodes","params":{"tvshowid":' + tvshow_id + ',"limits":{"end":100},"sort":{"method":"random"},"properties":["file"]' + xsp + '},"id":"get_random_tvshow"}')
                    if has_xsp == False:
                        xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title} All Seasons, NO RULES', xbmc.LOGINFO)
                    else:
                        xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title} All Seasons, RULES APPLIED', xbmc.LOGINFO)
            #Titles
            else: 
                tvshow_id = db_path[2]
                season_id = db_path[3]
                #Has valid season id
                if int(season_id) > -1:
                    video_list = xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"VideoLibrary.GetEpisodes","params":{"tvshowid":' + tvshow_id + ',"season":' + season_id + ',"limits":{"end":100},"sort":{"method":"random"},"properties":["file"]' + xsp + '},"id":"get_random_season"}')
                    if has_xsp == False:
                        xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title} 'f'{tvshow_season}, NO RULES', xbmc.LOGINFO)                                 
                    else:
                        xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title} 'f'{tvshow_season}, RULES APPLIED', xbmc.LOGINFO)
                        
                else: 
                    video_list = xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"VideoLibrary.GetEpisodes","params":{"tvshowid":' + tvshow_id + ',"limits":{"end":100},"sort":{"method":"random"},"properties":["file"]' + xsp + '},"id":"get_random_tvshow"}')
                    if has_xsp == False:
                        xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title} All Seasons, NO RULES', xbmc.LOGINFO)
                    else: 
                        xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title} All Seasons, RULES APPLIED', xbmc.LOGINFO)
    #Episodes
        if db_type == 'episode':
            #InProgress/RecentlyAdded
            if len(db_path)  <= 4:
                tvshow_id = db_path[1]
                season_id = db_path[2]
                #Has valid season id
                if int(season_id) > -1:
                    video_list = xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"VideoLibrary.GetEpisodes","params":{"tvshowid":' + tvshow_id + ',"season":' + season_id + ',"limits":{"end":100},"sort":{"method":"random"},"properties":["file"]' + xsp + '},"id":"get_random_season"}') 
                    if has_xsp == False:
                        xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title} 'f'{tvshow_season}, NO RULES', xbmc.LOGINFO)                                 

                else: 
                    video_list = xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"VideoLibrary.GetEpisodes","params":{"tvshowid":' + tvshow_id + ',"limits":{"end":100},"sort":{"method":"random"},"properties":["file"]' + xsp + '},"id":"get_random_tvshow"}') 
                    if has_xsp == False:
                        xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title}, NO RULES', xbmc.LOGINFO)                                 
            #Titles
            else: 
                tvshow_id = db_path[2]
                season_id = db_path[3]
                #Has valid show id
                if int(season_id) > -1:
                    video_list = xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"VideoLibrary.GetEpisodes","params":{"tvshowid":' + tvshow_id + ',"season":' + season_id + ',"limits":{"end":100},"sort":{"method":"random"},"properties":["file"]' + xsp + '},"id":"get_random_season"}') 
                    if has_xsp == False:
                        xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title} 'f'{tvshow_season}, NO RULES', xbmc.LOGINFO)
                    else:
                        xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title} 'f'{tvshow_season}, RULES APPLIED', xbmc.LOGINFO)
                #Has valid season id
                elif int(tvshow_id) == -1:
                    tvshow_id = tvshow_dbid
                    video_list = xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"VideoLibrary.GetEpisodes","params":{"tvshowid":' + tvshow_id + ',"limits":{"end":100},"sort":{"method":"random"},"properties":["file"]' + xsp + '},"id":"get_random_season"}')                       
                    if has_xsp == False:
                        xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title}, NO RULES', xbmc.LOGINFO)                          
                    else:
                        xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title}, RULES APPLIED', xbmc.LOGINFO)       
                else: 
                    video_list = xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"VideoLibrary.GetEpisodes","params":{"tvshowid":' + tvshow_id + ',"limits":{"end":100},"sort":{"method":"random"},"properties":["file"]' + xsp + '},"id":"get_random_tvshow"}')
                    if has_xsp == False:
                        xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title}, NO RULES', xbmc.LOGINFO)                                 
                    else:
                        xbmc.log('----(Playlist Resumer)...Playing random episodes from 'f'{tvshow_title}, RULES APPLIED', xbmc.LOGINFO)
    #Music Videos            
        if db_type == 'musicvideo':
            video_list = xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"VideoLibrary.GetMusicVideos","params":{"limits":{"end":100},"sort":{"method":"random"},"properties":["file"]' + xsp + '},"id":"get_random_music_video"}')
            if has_xsp == False:
                xbmc.log('----(Playlist Resumer)...Playing random music videos, NO RULES', xbmc.LOGINFO)
            else:
                xbmc.log('----(Playlist Resumer)...Playing random music videos, RULES APPLIED', xbmc.LOGINFO)        
#Add and play items
        json_response = json.loads(video_list)
        random_list = []
    #Movies
        if db_type == "movie":
            id_list = json_response['result']['movies']
            for item in id_list:
                str1 = '{"movieid":'
                str2 = str(item["movieid"])
                str3 = "}"
                str4 = "".join((str1, str2, str3))                    
                str5 = str4.replace("{", "").replace("}", "")
                if str5 not in str(ignored_ids) or list_item in str(ignored_ids):
                    random_list.append(str4)
                else:
                    xbmc.log('----(Playlist Resumer)...IGNORING VIDEO'f'{str4}', xbmc.LOGINFO)
    #TVShows, Seasons, Episodes            
        if db_type in ('episode', 'season', 'tvshow'):
            video_list = json_response['result']['episodes']
            for item in video_list:
                str1 = '{"episodeid":'
                str2 = str(item["episodeid"])
                str3 = "}"
                str4 = "".join((str1, str2, str3))
                str5 = str4.replace("{", "").replace("}", "")
                if str5 not in str(ignored_ids) or list_item in str(ignored_ids):
                    random_list.append(str4)
                else:
                    xbmc.log('----(Playlist Resumer)...IGNORING VIDEO'f'{str4}', xbmc.LOGINFO)
    #Music Videos
        if db_type == "musicvideo":
            video_list = json_response['result']['musicvideos']
            for item in video_list:
                str1 = '{"musicvideoid":'
                str2 = str(item["musicvideoid"])
                str3 = "}"
                str4 = "".join((str1, str2, str3))                    
                random_list.append(str4)
                str5 = str4.replace("{", "").replace("}", "")
                if str5 not in str(ignored_ids) or list_item in str(ignored_ids):
                    random_list.append(str4)
                else:
                    xbmc.log('----(Playlist Resumer)...IGNORING VIDEO'f'{str4}', xbmc.LOGINFO)                
        random_list = str(random_list)
        random_list = random_list.replace("'", "")
            
        xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Playlist.Clear","params":{"playlistid":1},"id":"playlist_clear"}')
        xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Playlist.Add","params":{"item":' + random_list + ',"playlistid":1},"id":"playlist_add"}')
        xbmc.sleep(100)       
        xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Player.Open","params":{"item":{"playlistid":1,"position":0}},"id":"player_open"}')             
#Playlists
    elif path_type == 'library':
        xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Player.Open","params":{"item":{"recursive":true, "directory":"' + path + '"},"options":{"shuffled":true}},"id":"play_playlist"}')
        xbmc.log('----(Playlist Resumer)...Playing random videos from 'f'{db_path[2]}', xbmc.LOGINFO)
    xbmc.executebuiltin('Dialog.Close(busydialognocancel)')