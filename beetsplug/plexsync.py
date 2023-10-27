"""Update and sync Plex music library.

Plex users enter the Plex Token to enable updating.
Put something like the following in your config.yaml to configure:
    plex:
        host: localhost
        token: token
"""

import asyncio
import difflib
import os
import re
import time

import confuse
import dateutil.parser
import requests
#import spotipy
from beets import config, ui
from beets.dbcore import types
from beets.dbcore.query import MatchQuery
from beets.library import DateType
from beets.plugins import BeetsPlugin
from beets.ui import input_, print_
#from bs4 import BeautifulSoup
from plexapi import exceptions
from plexapi.server import PlexServer
#from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth


class PlexSync(BeetsPlugin):
    """Define plexsync class."""
    data_source = 'Plex'

    item_types = {
        'plex_guid': types.STRING,
        'plex_ratingkey': types.INTEGER,
        'plex_userrating': types.FLOAT,
        'plex_skipcount': types.INTEGER,
        'plex_viewcount': types.INTEGER,
        'plex_lastviewedat': DateType(),
        'plex_lastratedat': DateType(),
        'plex_updated': DateType(),
    }

    class dotdict(dict):
        """dot.notation access to dictionary attributes"""
        __getattr__ = dict.get
        __setattr__ = dict.__setitem__
        __delattr__ = dict.__delitem__

    def __init__(self):
        """Initialize plexsync plugin."""
        super().__init__()

        self.config_dir = config.config_dir()

        # Adding defaults.
        config['plex'].add({
            'host': 'localhost',
            'port': 32400,
            'token': '',
            'library_name': 'Music',
            'secure': False,
            'ignore_cert_errors': False})

        config['plexsync'].add({
            'tokenfile': 'spotify_plexsync.json', #idk what this is but dont need spotify shit.
            'manual_search': False})
        self.plexsync_token = config['plexsync']['tokenfile'].get(
            confuse.Filename(in_app_dir=True)
        )


        config['plex']['token'].redact = True
        baseurl = "http://" + config['plex']['host'].get() + ":" \
            + str(config['plex']['port'].get())
        try:
            self.plex = PlexServer(baseurl,
                                   config['plex']['token'].get())
        except exceptions.Unauthorized:
            raise ui.UserError('Plex authorization failed')
        try:
            self.music = self.plex.library.section(
                config['plex']['library_name'].get())
        except exceptions.NotFound:
            raise ui.UserError(f"{config['plex']['library_name']} \
                library not found")
        self.register_listener('database_change', self.listen_for_db_change)



    def listen_for_db_change(self, lib, model):
        """Listens for beets db change and register the update for the end."""
        self.register_listener('cli_exit', self._plexupdate)

    def commands(self):
        """Add beet UI commands to interact with Plex."""
        plexupdate_cmd = ui.Subcommand(
            'plexupdate', help=f'Update {self.data_source} library')

        def func(lib, opts, args):
            self._plexupdate()

        plexupdate_cmd.func = func

        # plexsync command
        sync_cmd = ui.Subcommand('plexsync',
                                 help="fetch track attributes from Plex")
        sync_cmd.parser.add_option(
            '-f', '--force', dest='force_refetch',
            action='store_true', default=False,
            help='re-sync Plex data when already present'
        )

        def func_sync(lib, opts, args):
            items = lib.items(ui.decargs(args))
            self._fetch_plex_info(items, ui.should_write(),
                                  opts.force_refetch)
        sync_cmd.func = func_sync

        
        # plexsyncrecent command - instead of using the plexsync command which
        # can be slow, we can use the plexsyncrecent command to update info
        # for tracks played in the last 7 days.
        syncrecent_cmd = ui.Subcommand('plexsyncrecent',
                                       help="Sync recently played tracks")

        def func_sync_recent(lib, opts, args):
            self._update_recently_played(lib)

        syncrecent_cmd.func = func_sync_recent


        return [plexupdate_cmd, sync_cmd, syncrecent_cmd]

    def parse_title(self, title_orig):
        if "(From \"" in title_orig:
            title = re.sub(r'\(From.*\)', '', title_orig)
            album = re.sub(r'^[^"]+"|(?<!^)"[^"]+"|"[^"]+$', '', title_orig)
        elif "[From \"" in title_orig:
            title = re.sub(r'\[From.*\]', '', title_orig)
            album = re.sub(r'^[^"]+"|(?<!^)"[^"]+"|"[^"]+$', '', title_orig)
        else:
            title = title_orig
            album = ""
        return title, album

    def clean_album_name(self, album_orig):
        album_orig = album_orig.replace(
            "(Original Motion Picture Soundtrack)",
            "").replace("- Hindi", "").strip()
        if "(From \"" in album_orig:
            album = re.sub(r'^[^"]+"|(?<!^)"[^"]+"|"[^"]+$', '', album_orig)
        elif "[From \"" in album_orig:
            album = re.sub(r'^[^"]+"|(?<!^)"[^"]+"|"[^"]+$', '', album_orig)
        else:
            album = album_orig
        return album

    # Define a function to get playlist songs by id
    async def get_playlist_songs(playlist_url):
        # Use the async method from saavn
        songs = await saavn.get_playlist_songs(playlist_url)
        # Return a list of songs with details
        return songs


    # Define a function that takes a title string and a list of tuples as input
    def find_closest_match(self, title, lst):
        # Initialize an empty list to store the matches and their scores
        matches = []
        # Loop through each tuple in the list
        for t in lst:
            # Use the SequenceMatcher class to compare the title with the
            # first element of the tuple
            # The ratio method returns a score between 0 and 1 indicating how
            # similar the two strings are based on the Levenshtein distance
            score = difflib.SequenceMatcher(None, title, t.title).ratio()
            # Append the tuple and the score to the matches list
            matches.append((t, score))
        # Sort the matches list by the score in descending order
        matches.sort(key=lambda x: x[1], reverse=True)
        # Return only the first element of each tuple in the matches
        # list as a new list
        return [m[0] for m in matches]

    def _plexupdate(self):
        """Update Plex music library."""
        try:
            self.music.update()
            self._log.info('Update started.')
        except exceptions.PlexApiException:
            self._log.warning("{} Update failed",
                              self.config['plex']['library_name'])

    def _fetch_plex_info(self, items, write, force):
        """Obtain track information from Plex."""
        for index, item in enumerate(items, start=1):
            self._log.info('Processing {}/{} tracks - {} ',
                           index, len(items), item)
            # If we're not forcing re-downloading for all tracks, check
            # whether the popularity data is already present
            if not force:
                if 'plex_userrating' in item:
                    self._log.debug('Plex rating already present for: {}',
                                    item)
                    continue
            plex_track = self.search_plex_track(item)
            if plex_track is None:
                self._log.info('No track found for: {}', item)
                continue
            item.plex_guid = plex_track.guid
            item.plex_ratingkey = plex_track.ratingKey
            item.plex_userrating = plex_track.userRating
            item.plex_skipcount = plex_track.skipCount
            item.plex_viewcount = plex_track.viewCount
            item.plex_lastviewedat = plex_track.lastViewedAt
            item.plex_lastratedat = plex_track.lastRatedAt
            item.plex_updated = time.time()
            item.store()
            if write:
                item.try_write()

    def search_plex_track(self, item):
        """Fetch the Plex track key."""
        tracks = self.music.searchTracks(
            **{'album.title': item.album, 'track.title': item.title})
        if len(tracks) == 1:
            return tracks[0]
        elif len(tracks) > 1:
            for track in tracks:
                if track.parentTitle == item.album \
                   and track.title == item.title:
                    return track
        else:
            self._log.debug('Track {} not found in Plex library', item)
            return None

    def _update_recently_played(self, lib):
        """Fetch the Plex track key."""
        tracks = self.music.search(
            filters={'track.lastViewedAt>>': '7d'}, libtype='track')
        self._log.info("Updating information for {} tracks", len(tracks))
        with lib.transaction():
            for track in tracks:
                query = MatchQuery("plex_ratingkey", track.ratingKey,
                                   fast=False)
                items = lib.items(query)
                if not items:
                    self._log.debug("{} | track not found", query)
                    continue
                elif len(items) == 1:
                    self._log.info("Updating information for {} ", items[0])
                    try:
                        items[0].plex_userrating = track.userRating
                        items[0].plex_skipcount = track.skipCount
                        items[0].plex_viewcount = track.viewCount
                        items[0].plex_lastviewedat = track.lastViewedAt
                        items[0].plex_lastratedat = track.lastRatedAt
                        items[0].plex_updated = time.time()
                        items[0].store()
                        items[0].try_write()
                    except exceptions.NotFound:
                        self._log.debug("{} | track not found", items[0])
                        continue
                else:
                    self._log.debug("Please sync Plex library again")
                    continue

    def search_plex_song(self, song, manual_search=False):
        """Fetch the Plex track key."""

        try:
            if song['album'] is None:
                tracks = self.music.searchTracks(**{'track.title': song['title']})
            else:
                tracks = self.music.searchTracks(
                    **{'album.title': song['album'],
                       'track.title': song['title']})
                if len(tracks) == 0:
                    tracks = self.music.searchTracks(
                        **{'track.title': song['title']})
        except exceptions as e:
            self._log.debug('Error searching for {} - {}. Error: {}',
                            song['album'], song['title'], e)
            return None
        artist = song['artist'].split(",")[0]
        if len(tracks) == 1:
            return tracks[0]
        elif len(tracks) > 1:
            sorted_tracks = self.find_closest_match(song['title'], tracks)
            self._log.debug('Found {} tracks for {}', len(sorted_tracks),
                            song['title'])
            if manual_search and len(sorted_tracks) > 0:
                print_(f'Choose candidates for {song["album"]} - '
                       f'{song["title"]}:')
                for i, track in enumerate(sorted_tracks, start=1):
                    print_(f'{i}. {track.parentTitle} - {track.title} - '
                           f'{track.artist().title}')
                sel = ui.input_options(('aBort', 'Skip'),
                                       numrange=(1, len(sorted_tracks)),
                                       default=1)
                if sel in ('b', 'B', 's', 'S'):
                    return None
                return sorted_tracks[sel - 1] if sel > 0 else None
            for track in sorted_tracks:
                if track.originalTitle is not None:
                    plex_artist = track.originalTitle
                else:
                    plex_artist = track.artist().title
                if artist in plex_artist:
                    return track
        else:
            if config['plexsync']['manual_search'] and not manual_search:
                self._log.info('Track {} - {} not found in Plex',
                               song['album'], song['title'])
                if ui.input_yn("Search manually? (Y/n)"):
                    self.manual_track_search()
            else:
                self._log.info('Track {} - {} not found in Plex',
                               song['album'], song['title'])
            return None

    def manual_track_search(self):
        """Manually search for a track in the Plex library.

        Prompts the user to enter the title, album, and artist of the track
        they want to search for.
        Calls the `search_plex_song` method with the provided information and
        sets the `manual_search` flag to True.
        """
        song_dict = {}
        title = input_('Title:').strip()
        album = input_('Album:').strip()
        artist = input_('Artist:').strip()
        song_dict = {"title": title.strip(),
                     "album": album.strip(), "artist": artist.strip()}
        self.search_plex_song(song_dict, manual_search=True)
