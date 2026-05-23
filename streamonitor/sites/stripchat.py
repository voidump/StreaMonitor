import itertools
import json
import os.path
import random
import re
import time
import requests
import base64
import hashlib
import urllib.parse

from functools import lru_cache
from typing import Optional, Tuple

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from streamonitor.bot import RoomIdBot
from streamonitor.downloaders.hls import getVideoNativeHLS
from streamonitor.enums import Status, Gender, COUNTRIES


class StripChat(RoomIdBot):
    site = 'StripChat'
    siteslug = 'SC'

    bulk_update = True
    _static_data = None
    _main_js_data = None
    _doppio_js_data = None
    _mouflon_cache_filename = 'stripchat_mouflon_keys.json'
    # Pre-populated known keys + auto-extracted keys from doppio.js
    _mouflon_keys: dict = {
        "Zeechoej4aleeshi": "ubahjae7goPoodi6",
        "Zokee2OhPh9kugh4": "Quean4cai9boJa5a",
        "Ook7quaiNgiyuhai": "EQueeGh2kaewa3ch",
    }
    _cached_keys: dict[str, bytes] = None
    _session = None

    _DOPPIO_INDEX_PATTERN = re.compile(r'(\d+):\s*\"([a-f0-9]+)\"')
    _DOPPIO_REQUIRE_PATTERN = re.compile(r'require\([\"\']\./([Dd]oppio[^\"\']+\.js)[\"\']\)')
    _HASH_PATTERNS = [
        re.compile(r'{}:\\\"([a-zA-Z0-9]{{20}})\\\"'),
        re.compile(r'{}:\"([a-zA-Z0-9]{{20}})\"'),
        re.compile(r'\"{}\":\"([a-zA-Z0-9]{{20}})\"'),
    ]

    _MOUFLON_NEEDLE = "#EXT-X-MOUFLON:"
    _MOUFLON_FILE_ATTR = "#EXT-X-MOUFLON:FILE:"
    _MOUFLON_URI_ATTR = "#EXT-X-MOUFLON:URI:"
    _MOUFLON_FILENAME = "media.mp4"
    _CDN_DOMAINS = ("org", "com", "net")

    _PRIVATE_STATUSES = frozenset(["private", "groupShow", "p2p", "virtualPrivate", "p2pVoice"])
    _OFFLINE_STATUSES = frozenset(["off", "idle"])

    _GENDER_MAP = {
        'female': Gender.FEMALE,
        'male': Gender.MALE,
        'maleFemale': Gender.BOTH
    }

    # Load key cache from file if available
    if os.path.exists(_mouflon_cache_filename):
        with open(_mouflon_cache_filename) as f:
            try:
                _mouflon_keys.update(json.load(f))
                print('Loaded StripChat mouflon key cache')
            except Exception as e:
                print('Error loading mouflon key cache:', e)

    def __init__(self, username, room_id=None):
        if StripChat._static_data is None:
            StripChat._static_data = {}
            try:
                self.getInitialData()
            except Exception as e:
                print(f'Error initializing StripChat static data: {e}')
                StripChat._static_data = None

        # Wait for static data initialization with timeout
        end_time = time.time() + 15
        while StripChat._static_data == {} and time.time() < end_time:
            time.sleep(0.01)

        super().__init__(username, room_id)
        self._id = None
        self.vr = False
        self.getVideo = lambda _, url, filename: getVideoNativeHLS(self, url, filename, StripChat.m3u_decoder)

    @classmethod
    def _get_session(cls):
        """Get or create a shared session with connection pooling and retry logic."""
        if cls._session is None:
            cls._session = requests.Session()
            retry = Retry(
                total=2,
                backoff_factor=0.1,
                status_forcelist=[429, 500, 502, 503, 504],
            )
            adapter = HTTPAdapter(
                max_retries=retry,
                pool_connections=15,
                pool_maxsize=30,
                pool_block=False
            )
            cls._session.mount("http://", adapter)
            cls._session.mount("https://", adapter)
            cls._session.headers.update({
                'Connection': 'keep-alive',
                'Accept-Encoding': 'gzip, deflate'
            })
        return cls._session

    @classmethod
    def getInitialData(cls):
        """Fetch static config and doppio.js for automatic key extraction."""
        s = cls._get_session()

        try:
            r = s.get(
                "https://hu.stripchat.com/api/front/v3/config/static",
                headers=cls.headers,
                timeout=5
            )
            r.raise_for_status()
            response_json = r.json()

            # Handle different response structures
            if "static" in response_json:
                static_data = response_json["static"]
            else:
                static_data = response_json

            # Determine MMP origin - try featureSettings first, then features
            if "featureSettings" in static_data:
                mmp_origin = "https://mmp.doppiocdn.com/player/mmp"
            elif "features" in static_data:
                mmp_origin = "https://mmp.doppiocdn.com/player/mmp"
            else:
                raise Exception(f"'featureSettings'/'features' not found. Keys: {list(static_data.keys())}")

            if "featuresV2" not in static_data:
                raise Exception(f"'featuresV2' not found. Keys: {list(static_data.keys())}")

            if "playerModuleExternalLoading" not in static_data["featuresV2"]:
                raise Exception("'playerModuleExternalLoading' not found in featuresV2")

            mmp_version = static_data["featuresV2"]["playerModuleExternalLoading"]["mmpVersion"]
            mmp_base = f"{mmp_origin}/{mmp_version}"

            # Fetch main.js to find doppio.js reference
            r = s.get(f"{mmp_base}/main.js", headers=cls.headers, timeout=5)
            r.raise_for_status()
            main_js_data = r.text

            doppio_url = None

            # Try require() pattern first
            if match := cls._DOPPIO_REQUIRE_PATTERN.search(main_js_data):
                doppio_url = f"{mmp_base}/{match[1]}"
            # Then try chunk-based pattern
            elif match := cls._DOPPIO_INDEX_PATTERN.search(main_js_data):
                idx = match[1]
                for pattern_template in cls._HASH_PATTERNS:
                    pattern = re.compile(pattern_template.pattern.format(idx))
                    if hash_match := pattern.search(main_js_data):
                        doppio_url = f"{mmp_base}/chunk-{hash_match[1]}.js"
                        break

            if not doppio_url:
                print("WARNING: Doppio.js not found, key auto-extraction disabled")
            else:
                r = s.get(doppio_url, headers=cls.headers, timeout=5)
                r.raise_for_status()
                StripChat._doppio_js_data = r.text
                print(f"Loaded doppio.js for auto key extraction")

            # Only update class variables after everything succeeds
            StripChat._static_data = static_data
            StripChat._main_js_data = main_js_data

        except Exception as e:
            print(f"ERROR in getInitialData: {e}")
            # Fall back to basic static data fetch
            try:
                session = requests.Session()
                r = session.get('https://stripchat.com/api/front/v3/config/static', headers=cls.headers)
                if r.status_code == 200:
                    StripChat._static_data = r.json().get('static')
            except:
                pass

    @classmethod
    @lru_cache(maxsize=512)
    def _get_hash_bytes(cls, key: str) -> bytes:
        """Cached SHA256 hash computation for XOR decryption."""
        return hashlib.sha256(key.encode("utf-8")).digest()

    @classmethod
    def m3u_decoder(cls, content):
        """Decode mouflon-encrypted M3U8 playlists (V1 and V2 support)."""

        @lru_cache(maxsize=64)
        def _decode(encrypted_b64: str, key: str) -> str:
            hash_bytes = cls._get_hash_bytes(key)
            encrypted_data = base64.b64decode(encrypted_b64 + "==")
            return bytes(a ^ b for (a, b) in zip(encrypted_data, itertools.cycle(hash_bytes))).decode("utf-8")

        psch, pkey, pdkey = StripChat._getMouflonFromM3U(content)

        if not pdkey:
            return content

        lines = content.split('\n')
        decoded = []
        last_decoded_file = None

        if psch == 'v1':
            # V1: FILE attribute decoding
            for line in lines:
                if line.startswith(cls._MOUFLON_FILE_ATTR):
                    last_decoded_file = _decode(line[len(cls._MOUFLON_FILE_ATTR):], pdkey)
                elif last_decoded_file and line.endswith(cls._MOUFLON_FILENAME):
                    decoded.append(line.replace(cls._MOUFLON_FILENAME, last_decoded_file))
                    last_decoded_file = None
                else:
                    decoded.append(line)

        elif psch == 'v2':
            # V2: URI attribute decoding with init segment support
            i = 0
            while i < len(lines):
                line = lines[i]
                line_stripped = line.strip()

                # Handle #EXT-X-MAP:URI lines (initialization segment)
                if line_stripped.startswith('#EXT-X-MAP:URI'):
                    try:
                        if '"' in line:
                            start_quote = line.find('"')
                            end_quote = line.rfind('"')
                            if start_quote >= 0 and end_quote > start_quote:
                                map_uri = line[start_quote+1:end_quote]
                                if '_init_' in map_uri and '.mp4' in map_uri:
                                    uri_without_ext = map_uri[:-4]
                                    last_us = uri_without_ext.rfind('_')
                                    if last_us > 0:
                                        url_before_encrypted = uri_without_ext[:last_us]
                                        encrypted_part = uri_without_ext[last_us+1:]
                                        reversed_enc = encrypted_part[::-1]
                                        decoded_part = _decode(reversed_enc, pdkey)
                                        new_map_uri = f'{url_before_encrypted}_{decoded_part}.mp4'
                                        decoded.append(f'#EXT-X-MAP:URI="{new_map_uri}"')
                                        i += 1
                                        continue
                    except Exception:
                        pass
                    decoded.append(line)
                    i += 1
                    continue

                # Handle segment URI lines
                if line.startswith(cls._MOUFLON_URI_ATTR):
                    uri_value = line[len(cls._MOUFLON_URI_ATTR):]
                    try:
                        if '.mp4' in uri_value:
                            last_underscore = uri_value.rfind('_')
                            if last_underscore > 0:
                                url_without_timestamp = uri_value[:last_underscore]
                                timestamp_part = uri_value[last_underscore+1:]
                                second_last_underscore = url_without_timestamp.rfind('_')
                                if second_last_underscore > 0:
                                    url_before_encrypted = url_without_timestamp[:second_last_underscore]
                                    encrypted = url_without_timestamp[second_last_underscore+1:]
                                    reversed_encrypted = encrypted[::-1]
                                    decoded_segment = _decode(reversed_encrypted, pdkey)
                                    decoded_uri = f"{url_before_encrypted}_{decoded_segment}_{timestamp_part}"
                                    i += 1
                                    if i < len(lines):
                                        next_line = lines[i]
                                        if 'media.mp4' in next_line:
                                            decoded.append(decoded_uri)
                                            i += 1
                                            continue
                                        else:
                                            decoded.append(decoded_uri)
                                            continue
                                    else:
                                        decoded.append(decoded_uri)
                                        break
                    except Exception:
                        pass
                    i += 1
                    continue
                else:
                    decoded.append(line)
                i += 1
        else:
            return content

        return '\n'.join(decoded)

    @classmethod
    @lru_cache(maxsize=128)
    def getMouflonDecKey(cls, pkey) -> Optional[str]:
        """Get mouflon decryption key - first from cache, then auto-extract from doppio.js."""
        if cls._mouflon_keys and pkey in cls._mouflon_keys:
            return cls._mouflon_keys[pkey]

        # Auto-extract key from doppio.js
        if cls._doppio_js_data:
            pattern = f'"{pkey}:'
            idx = cls._doppio_js_data.find(pattern)
            if idx != -1:
                start = idx + len(pattern)
                end = cls._doppio_js_data.find('"', start)
                if end != -1:
                    key = cls._doppio_js_data[start:end]
                    if cls._mouflon_keys is None:
                        cls._mouflon_keys = {}
                    cls._mouflon_keys[pkey] = key
                    print(f'Auto-extracted mouflon key for pkey={pkey[:8]}...')
                    return key

        return None

    @staticmethod
    def _getMouflonFromM3U(m3u8_doc) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Extract mouflon encryption parameters from M3U8 document."""
        needle = StripChat._MOUFLON_NEEDLE
        idx = 0

        while (idx := m3u8_doc.find(needle, idx)) != -1:
            line_end = m3u8_doc.find('\n', idx)
            if line_end == -1:
                line_end = len(m3u8_doc)

            line = m3u8_doc[idx:line_end].strip()
            parts = line.split(':', 3)

            if len(parts) >= 4:
                psch, pkey = parts[2], parts[3]
                if pdkey := StripChat.getMouflonDecKey(pkey):
                    return psch, pkey, pdkey

            idx += len(needle)

        return None, None, None

    def getWebsiteURL(self):
        return "https://stripchat.com/" + self.username

    def getVideoUrl(self):
        return self.getWantedResolutionPlaylist(None)

    @staticmethod
    def _strip_playlist_type(url):
        """Strip playlistType param from URL to avoid ADVERT playlist responses."""
        parsed = urllib.parse.urlparse(url)
        if not parsed.query:
            return url
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        params.pop('playlistType', None)
        new_query = urllib.parse.urlencode(params, doseq=True)
        return urllib.parse.urlunparse((
            parsed.scheme, parsed.netloc, parsed.path,
            parsed.params, new_query, parsed.fragment
        ))

    def getPlaylistVariants(self, url):
        url = "https://edge-hls.{host}/hls/{id}{vr}/master/{id}{vr}{auto}.m3u8".format(
                host='doppiocdn.' + random.choice(list(self._CDN_DOMAINS)),
                id=self.room_id,
                vr='_vr' if self.vr else '',
                auto='_auto' if not self.vr else ''
            )
        try:
            result = self.session.get(url, headers=self.headers, cookies=self.cookies, timeout=4)
            result.raise_for_status()
        except Exception:
            return []

        m3u8_doc = result.content.decode("utf-8")
        psch, pkey, pdkey = StripChat._getMouflonFromM3U(m3u8_doc)
        if pdkey is None:
            self.log('Failed to get mouflon decryption key')
            return []
        variants = super().getPlaylistVariants(m3u_data=m3u8_doc)
        # Strip playlistType=standard from variant URLs (causes ADVERT responses),
        # normalize CDN domain to .net (avoid .com SSL issues),
        # and append psch/pkey for mouflon decryption
        result_variants = []
        for variant in variants:
            clean_url = self._strip_playlist_type(variant['url'])
            # Normalize CDN domain - media-hls.doppiocdn.com has SSL issues
            clean_url = clean_url.replace('doppiocdn.com', 'doppiocdn.net')
            clean_url = clean_url.replace('doppiocdn.org', 'doppiocdn.net')
            sep = '&' if '?' in clean_url else '?'
            variant_url = f'{clean_url}{sep}psch={psch}&pkey={pkey}'
            result_variants.append(variant | {'url': variant_url})
        return result_variants

    @staticmethod
    def uniq(length=16):
        return ''.join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=length))

    def _getStatusData(self, username):
        r = self.session.get(
            f'https://stripchat.com/api/front/v2/models/username/{username}/cam?uniq={StripChat.uniq()}',
            headers=self.headers,
            timeout=4
        )

        try:
            data = r.json()
        except requests.exceptions.JSONDecodeError:
            self.log('Failed to parse JSON response')
            return None
        return data

    def _update_lastInfo(self, data):
        if data is None:
            return None
        if 'cam' not in data:
            if 'error' in data:
                error = data['error']
                if error == 'Not Found':
                    return Status.NOTEXIST
                self.logger.warn(f'Status returned error: {error}')
            return Status.UNKNOWN

        self.lastInfo = {'model': data['user']['user']}
        if isinstance(data['cam'], dict):
            self.lastInfo |= data['cam']
        return None

    def getRoomIdFromUsername(self, username):
        if username == self.username and self.room_id is not None:
            return self.room_id

        data = self._getStatusData(username)
        if username == self.username:
            self._update_lastInfo(data)

        if 'user' not in data:
            return None
        if 'user' not in data['user']:
            return None
        if 'id' not in data['user']['user']:
            return None

        return str(data['user']['user']['id'])

    def getStatus(self):
        data = self._getStatusData(self.username)
        if data is None:
            return Status.UNKNOWN

        error = self._update_lastInfo(data)
        if error:
            return error

        if 'user' in data and 'user' in data['user']:
            model_data = data['user']['user']
            if model_data.get('gender'):
                self.gender = StripChat._GENDER_MAP.get(model_data.get('gender'))

            if model_data.get('country'):
                self.country = model_data.get('country', '').upper()
            elif model_data.get('languages'):
                for lang in model_data['languages']:
                    if lang.upper() in COUNTRIES:
                        self.country = lang.upper()
                        break

        status = self.lastInfo['model'].get('status')
        if status == "public" and self.lastInfo["isCamAvailable"] and self.lastInfo["isCamActive"]:
            return Status.PUBLIC
        if status in self._PRIVATE_STATUSES:
            return Status.PRIVATE
        if status in self._OFFLINE_STATUSES:
            return Status.OFFLINE
        if self.lastInfo['model'].get('isDeleted') is True:
            return Status.NOTEXIST
        if data['user'].get('isGeoBanned') is True:
            return Status.RESTRICTED
        self.logger.warn(f'Got unknown status: {status}')
        return Status.UNKNOWN

    @classmethod
    def getStatusBulk(cls, streamers):
        model_ids = {}
        for streamer in streamers:
            if not isinstance(streamer, StripChat):
                continue
            if streamer.room_id:
                model_ids[streamer.room_id] = streamer

        base_url = 'https://stripchat.com/api/front/models/list?'
        batch_num = 100
        data_map = {}
        model_id_list = list(model_ids)
        for _batch_ids in [model_id_list[i:i+batch_num] for i in range(0, len(model_id_list), batch_num)]:
            session = requests.Session()
            session.headers.update(cls.headers)
            # Retry with increased timeout for transient network issues
            for attempt in range(2):
                try:
                    r = session.get(base_url + '&'.join(f'modelIds[]={model_id}' for model_id in _batch_ids), timeout=20)
                    break
                except requests.exceptions.Timeout:
                    if attempt == 0:
                        continue
                    raise

            try:
                data = r.json()
            except requests.exceptions.JSONDecodeError:
                print('Failed to parse JSON response')
                return
            data_map |= {str(model['id']): model for model in data.get('models', [])}

        for model_id, streamer in model_ids.items():
            model_data = data_map.get(model_id)
            if not model_data:
                streamer.setStatus(Status.UNKNOWN)
                continue
            if model_data.get('gender'):
                streamer.gender = cls._GENDER_MAP.get(model_data.get('gender'))
            if model_data.get('country'):
                streamer.country = model_data.get('country', '').upper()
            status = model_data.get('status')
            if status == "public":
                streamer.setStatus(Status.PUBLIC)
            elif status in cls._PRIVATE_STATUSES:
                streamer.setStatus(Status.PRIVATE)
            elif status in cls._OFFLINE_STATUSES:
                streamer.setStatus(Status.OFFLINE)
            else:
                print(f'[{streamer.siteslug}] {streamer.username}: Bulk update got unknown status: {status}')
                streamer.setStatus(Status.UNKNOWN)
