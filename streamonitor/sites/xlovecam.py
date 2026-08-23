import requests
from streamonitor.bot import Bot
from streamonitor.enums import Status
from streamonitor.http_session import DEFAULT_TIMEOUT


class XLoveCam(Bot):
    site = 'XLoveCam'
    siteslug = 'XLC'

    def __init__(self, username):
        # No network in the constructor: a transient failure here used to
        # propagate into loadStreamers, which dropped the entry and let the
        # next saveConfig erase it from config.json. The performer id resolves
        # lazily on the first status poll instead.
        self._id = None
        super().__init__(username)

    def _resolve_performer_id(self):
        # Returns the id, or None when the performer is not in the online
        # list. The endpoint only returns online performers, so absence means
        # "offline or nonexistent" — indistinguishable, and therefore never
        # grounds for NOTEXIST. Network/HTTP errors raise so the caller can
        # treat them as transient.
        data = {
            'config[nickname]':	self.username,
            'config[favorite]':	"0",
            'config[recent]': "0",
            'config[vip]': "0",
            'config[sort][id]':	"35",
            'offset[from]':	"0",
            'offset[length]': "35",
            'origin': "filter-chg",
            'stat':	"0",
        }
        r = requests.post('https://www.xlovecam.com/hu/performerAction/onlineList', headers=self.headers, data=data, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        resp = r.json()
        performers = resp.get('content', {}).get('performerList') or []
        for performer in performers:
            if performer['nickname'].lower() == self.username.lower():
                return performer['id']
        return None

    def getVideoUrl(self):
        return self.lastInfo.get('hlsPlaylistFree')

    def getStatus(self):
        if self._id is None:
            try:
                self._id = self._resolve_performer_id()
            except Exception as e:
                self.logger.warning(f'Performer id lookup failed (will retry): {e}')
                return Status.UNKNOWN
            if self._id is None:
                # Not in the online list: offline (or nonexistent — the site
                # gives no way to tell). Keep polling instead of stopping the
                # monitor with NOTEXIST.
                return Status.OFFLINE

        data = {
            'performerId': self._id,
        }
        r = requests.post(f'https://www.xlovecam.com/hu/performerAction/getPerformerRoom', headers=self.headers, data=data, timeout=DEFAULT_TIMEOUT)

        if not r.ok:
            return Status.UNKNOWN
        if 'content' not in r.json():
            return Status.UNKNOWN
        if 'performer' not in r.json()['content']:
            return Status.UNKNOWN
        self.lastInfo = r.json()['content']['performer']

        if not self.lastInfo.get('enabled'):
            return Status.NOTEXIST
        if self.lastInfo.get('online') == 1:
            if 'hlsPlaylistFree' not in self.lastInfo:
                return Status.UNKNOWN
            url = self.lastInfo.get('hlsPlaylistFree')
            r_m3u = self.session.get(url)
            m3u_data = r_m3u.content
            if m3u_data.startswith(b'{') or b'EXTM3U' not in m3u_data:
                return Status.PRIVATE
            return Status.PUBLIC
        if self.lastInfo.get('online') == 0:
            return Status.OFFLINE
        return Status.UNKNOWN
