import re
import urllib.request

url = "https://cdn.smartcat.com/web/assets/index-vd9dy0KW.js"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
data = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
patterns = sorted(set(re.findall(r'["\'](/api/[^"\']+)["\']', data)))
for p in patterns:
    if any(k in p.lower() for k in ("project", "document", "file", "drive", "export", "translat")):
        print(p)
print("TOTAL", len(patterns))
