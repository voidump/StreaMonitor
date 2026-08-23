import requests
from streamonitor.bot import Bot
from streamonitor.enums import Status


class StreaMate(Bot):
    site = 'StreaMate'
    siteslug = 'SM'
    aliases = ['pornhublive']

    def getWebsiteURL(self):
        return "https://streamate.com/cam/" + self.username

    def getPlaylistVariants(self, url):
        sources = []
        # formats: mp4-rtmp, mp4-hls, mp4-ws
        for source in self.lastInfo['formats']['mp4-hls']['encodings']:
            sources.append({
                'url': source['location'],
                'resolution': (source['videoWidth'], source['videoHeight']),
                'frame_rate': None,
                'bandwidth': None
            })
        return sources

    def getVideoUrl(self):
        return self.getWantedResolutionPlaylist(None)

    def getStatus(self):
        headers = self.headers | {
            'Content-Type': 'application/json',
            'Referer': 'https://streamate.com/'
        }
        body = self._fetch_json(
            'GET',
            'https://manifest-server.naiadsystems.com/live/s:' + self.username + '.json?last=load&format=mp4-hls',
            headers=headers)
        if body is not None:
            self.lastInfo = body
            return Status.PUBLIC
        # 200 with an unparseable body (CDN challenge page, truncated
        # response) is a transport problem, not a room state — mapping it
        # through the enum would call it PUBLIC with a stale lastInfo and
        # start doomed recording attempts.
        if self._last_fetch_status == 200:
            return Status.UNKNOWN
        # Historical mapping: this endpoint expresses the room state through
        # HTTP codes that happen to match the Status enum values (403 private,
        # 404 offline, ...). Codes outside the enum (500/502/301) used to
        # raise ValueError; they are transient -> UNKNOWN.
        try:
            return Status(self._last_fetch_status)
        except ValueError:
            return Status.UNKNOWN
