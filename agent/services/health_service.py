import urllib.request, urllib.error
class HealthService:
    def check(self,url,timeout=15):
        try:
            req=urllib.request.Request(url, method='GET')
            with urllib.request.urlopen(req,timeout=timeout) as r:
                return 200 <= r.status < 400, f'HTTP {r.status}'
        except Exception as e:
            return False, str(e)
