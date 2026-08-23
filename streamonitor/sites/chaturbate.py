import re
import requests
from streamonitor.bot import Bot
from streamonitor.enums import Status, Gender
from streamonitor.http_session import DEFAULT_TIMEOUT


class Chaturbate(Bot):
    site = 'Chaturbate'
    siteslug = 'CB'
    bulk_update = True

    _GENDER_MAP = {
        'f': Gender.FEMALE,
        'm': Gender.MALE,
        's': Gender.TRANS,
        'c': Gender.BOTH,
    }

    def __init__(self, username):
        super().__init__(username)
        self.sleep_on_offline = 30
        self.sleep_on_error = 60
    
    def getWebsiteURL(self):
        return "https://www.chaturbate.com/" + self.username
    
    def getVideoUrl(self):
        if self.bulk_update:
            self.getStatus()
        url = self.lastInfo['url']
        if not url:
            return None
        if self.lastInfo.get('cmaf_edge'):
            url = url.replace('playlist.m3u8', 'playlist_sfm4s.m3u8')
            url = re.sub('live-.+amlst', 'live-c-fhls/amlst', url)

        return self.getWantedResolutionPlaylist(url)
    
    @staticmethod
    def _parseStatus(status):
        if status == "public":
            return Status.PUBLIC
        elif status in ["private", "hidden"]:
            return Status.PRIVATE
        else:
            return Status.OFFLINE

    def getStatus(self):
        headers = {"X-Requested-With": "XMLHttpRequest"}
        data = {"room_slug": self.username, "bandwidth": "high"}

        body = self._fetch_json('POST', "https://chaturbate.com/get_edge_hls_url_ajax/",
                                headers=headers, data=data, timeout=DEFAULT_TIMEOUT)
        if body is None:
            # Only an actual 429 means rate-limited; DNS failures, JSON changes
            # and 5xx used to be swallowed by a bare except into RATELIMIT,
            # masking real breakage behind a 180s backoff.
            status = Status.RATELIMIT if self._last_fetch_status == 429 else Status.UNKNOWN
            self.ratelimit = status == Status.RATELIMIT
            return status
        self.lastInfo = body
        try:
            status = self._parseStatus(body['room_status'])
        except (KeyError, TypeError):
            self.logger.warning('Unexpected response shape from edge_hls endpoint')
            self.ratelimit = False
            return Status.UNKNOWN
        if status == Status.PUBLIC and not body.get('url'):
            status = Status.RESTRICTED

        self.ratelimit = status == Status.RATELIMIT
        return status

    @classmethod
    def getStatusBulk(cls, streamers):
        session = requests.Session()
        session.headers.update(cls.headers)
        try:
            r = session.get("https://chaturbate.com/affiliates/api/onlinerooms/?format=json&wm=DkfRj", timeout=10)
            data = r.json()
        except Exception as e:
            print(f'Chaturbate bulk status failed: {e}')
            return
        data_map = {str(model['username']).lower(): model for model in data}

        for streamer in streamers:
            model_data = data_map.get(streamer.username.lower())
            if not model_data:
                streamer.setStatus(Status.OFFLINE)
                continue
            if model_data.get('gender'):
                streamer.gender = cls._GENDER_MAP.get(model_data.get('gender'))
            if model_data.get('country'):
                streamer.country = model_data.get('country', '').upper()
            status = cls._parseStatus(model_data['current_show'])
            if status == status.PUBLIC:
                if streamer.sc in [status.PUBLIC, Status.RESTRICTED]:
                    continue
                status = streamer.getStatus()
            if status == Status.UNKNOWN:
                print(f'[{streamer.siteslug}] {streamer.username}: Bulk update got unknown status: {status}')
            streamer.setStatus(status)
