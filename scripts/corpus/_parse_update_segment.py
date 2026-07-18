import re
import urllib.request

data = urllib.request.urlopen(
    "https://cdn.smartcat.com/editor/assets/index-DL0-JaJZ.js", timeout=120
).read().decode("utf-8", errors="replace")

# Find updateSegment method definitions and nearby api urls
for match in re.finditer(r".{0,200}updateSegment.{0,200}", data):
    snippet = match.group().replace("\n", " ")
    if "api/" in snippet or "method:" in snippet or "url:" in snippet:
        print(snippet[:400])
        print("----")

print("\n==== put/post with saveType params ====")
for match in re.finditer(
    r"baseURL.*?url:.*?method:.*?saveType",
    data,
):
    print(match.group().replace("\n", " ")[:400])
    print("----")

# Look for template urls with SegmentTargets and id
for match in re.finditer(r".{0,40}api/Segments/Batch/SegmentTargets.\{0,120}", data):
    print(match.group().replace("\n", " ")[:250])
