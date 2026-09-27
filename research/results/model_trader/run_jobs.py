import json, sys, urllib.request
src, dst = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as fh, open(dst + ".part", "w", encoding="utf-8") as out:
    for line in fh:
        job = json.loads(line)
        try:
            req = urllib.request.Request("http://127.0.0.1:8788/completion", data=json.dumps(job).encode(),
                                         headers={"Content-Type": "application/json"})
            content = json.loads(urllib.request.urlopen(req, timeout=1800).read()).get("content", "")
        except Exception as exc:
            content = "ERROR " + str(exc)
        out.write(json.dumps({"content": content}, ensure_ascii=False) + "\n")
        out.flush()
import os
os.replace(dst + ".part", dst)
