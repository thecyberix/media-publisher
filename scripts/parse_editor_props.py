from __future__ import annotations

import html
import json
import re
from pathlib import Path

text = Path("downloads/happyscribe/debug-export.html").read_text(encoding="utf-8")
match = re.search(r'id="editor"[^>]*data-props="([^"]+)"', text)
data = json.loads(html.unescape(match.group(1)))

def walk(obj, path=""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            new_path = f"{path}.{key}" if path else key
            if any(token in key.lower() for token in ("export", "video", "hard", "burn", "download")):
                print(new_path, "=", str(value)[:300])
            walk(value, new_path)
    elif isinstance(obj, list):
        for index, value in enumerate(obj[:5]):
            walk(value, f"{path}[{index}]")

walk(data)
