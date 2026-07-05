import requests

# Applied to every request that doesn't specify its own timeout, so a hung
# site endpoint can't block a bot thread forever.
DEFAULT_TIMEOUT = 15


class TimeoutSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault('timeout', DEFAULT_TIMEOUT)
        return super().request(method, url, **kwargs)
