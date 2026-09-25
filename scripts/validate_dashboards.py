"""Run every panel + annotation query in the given dashboard JSON files against the live DB.

Catches SQL errors before Grafana shows a broken panel. Grafana macros are substituted
for a 30-day window. Each query runs in its own rolled-back transaction with a timeout.
Checks: executes, row count, time-series queries return a `time` column.

Usage (inside bot_copy):  python scripts/validate_dashboards.py /tmp/dash/*.json
Exit code 1 if any query errors.
"""
import json
import re
import sys

from sqlalchemy import text

from framework.db import session_scope

WIN = "interval '30 days'"


def subst(sql: str) -> str:
    sql = re.sub(r"\$__timeFilter\(([^)]+)\)", rf"(\1 BETWEEN now() - {WIN} AND now())", sql)
    sql = sql.replace("$__timeFrom()", f"(now() - {WIN})").replace("$__timeTo()", "now()")
    return sql


def queries(d):
    for p in d.get("panels", []):
        for q in p.get("panels", []) or []:
            yield from _panel(q)
        yield from _panel(p)
    for a in d.get("annotations", {}).get("list", []):
        t = a.get("target") or {}
        if t.get("rawSql"):
            yield f"[annotation] {a.get('name')}", t["rawSql"], t.get("format", "table")


def _panel(p):
    for t in p.get("targets", []) or []:
        if t.get("rawSql"):
            yield p.get("title") or f"<{p.get('type')}>", t["rawSql"], t.get("format", "table")


def main(paths):
    bad = 0
    for path in paths:
        d = json.load(open(path, encoding="utf-8"))
        print(f"\n=== {path.split('/')[-1]}  ({d.get('title')})")
        for title, sql, fmt in queries(d):
            with session_scope() as s:
                try:
                    s.execute(text("SET LOCAL statement_timeout = '20s'"))
                    res = s.execute(text(subst(sql)))
                    cols = list(res.keys())
                    rows = res.fetchall()
                    s.rollback()
                    note = ""
                    if fmt == "time_series" and "time" not in cols:
                        note = "  !! time_series without 'time' column"; bad += 1
                    elif not rows:
                        note = "  (0 rows)"
                    first = ""
                    if rows and len(rows) <= 3 or (rows and fmt == "table" and len(cols) <= 3):
                        first = "  e.g. " + str(tuple(rows[0]))[:110]
                    print(f"  OK   {len(rows):5d} rows  {title[:70]}{note}{first}")
                except Exception as e:
                    s.rollback()
                    bad += 1
                    msg = str(getattr(e, "orig", e)).split("\n")[0][:200]
                    print(f"  ERR  {title[:70]}\n       -> {msg}")
    print(f"\n{'FAIL' if bad else 'PASS'}: {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
