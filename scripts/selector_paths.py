"""Price paths for the Phase-2 selector study tokens, so the fleet's real exit stack can be replayed.

For every token in the study OUT file (scripts/selector_study.py), fetch from Birdeye:
  m1   1-minute prices t0 .. t0+12h   (the fleet's 12h timeout; a -8% stop needs fine bars)
  m15  15-minute prices t0 .. t0+7d   (swing-style / longer-hold variants)
Rows: {"token", "t0", "m1": [[unix, price], ...], "m15": [[unix, price], ...]}
Resumable (appends to PATHS_OUT, skips done tokens). Read-only.
Run in a container with the Birdeye key:
  STUDY=/tmp/selector_study.jsonl PATHS_OUT=/tmp/selector_paths.jsonl python scripts/selector_paths.py
"""
import json
import os
import time
from datetime import datetime

from scripts.selector_study import hist

STUDY = os.getenv("STUDY", "/tmp/selector_study.jsonl")
OUT = os.getenv("PATHS_OUT", "/tmp/selector_paths.jsonl")


def main():
    rows = [json.loads(l) for l in open(STUDY) if l.strip()]
    rows = [r for r in rows if "hit2x_first" in r]
    done = set()
    if os.path.exists(OUT):
        done = {json.loads(l)["token"] for l in open(OUT) if l.strip()}
    todo = [r for r in rows if r["token"] not in done]
    print(f"{len(rows)} study tokens, {len(done)} done, {len(todo)} to go", flush=True)
    out = open(OUT, "a")
    for i, r in enumerate(todo, 1):
        t0u = int(datetime.fromisoformat(r["t0"]).timestamp())
        m1 = hist(r["token"], t0u - 120, t0u + 12 * 3600 + 60, "1m")
        time.sleep(0.12)
        m15 = hist(r["token"], t0u - 900, t0u + 7 * 86400 + 900, "15m")
        time.sleep(0.12)
        out.write(json.dumps({"token": r["token"], "t0": r["t0"], "m1": m1, "m15": m15}) + "\n")
        out.flush()
        if i % 100 == 0:
            print(f"  {i}/{len(todo)}", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
