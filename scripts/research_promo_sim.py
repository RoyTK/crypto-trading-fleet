"""Gate-4 sim: buy at Dexscreener paid-promo payment, exit via the cluster stack.

Strategy simulated: see a paid promo order on a token -> buy at the next hourly
price (Birdeye 1H points, so entry latency is up to 1h = conservative), 150bps
slippage each way, then the cluster mechanical exit stack:
  - 25% partials at 4x / 10x / 50x / 1000x of entry
  - 45% multiplicative trailing stop after +20% activation (exit remaining)
  - hard stop on remaining (two variants: 8% tight / 30% widened, bracketing the
    liquidity-momentum rule which widens to 30% when liquidity is growing)
  - timeout liquidation (two variants: 12h faithful-to-cluster / 72h promo-tuned,
    since promo's median lead to ignition is ~13h and a 12h timeout can exit
    BEFORE the run starts)
Pessimistic ordering within each hourly step: timeout -> hard stop -> trailing ->
partials (a stop and a rally in the same hour resolves as the stop).

HONESTY CAP: the corpus is RUNNERS-ONLY, so results are conditional on the token
eventually running = an UPPER BOUND on live EV. Gate-4 reads it as
necessary-not-sufficient: if promo-entry can't make money even on runners, the
source dies here; if it can, Phase 2's shadow collector (which logs duds) decides.
Split reported by band: 'adjacent' (first promo within -3d..+1d of run) vs 'stale'
(first promo 4-638d pre-run — buying those means holding long before this run).

Run:  docker compose exec -T framework python -m scripts.research_promo_sim
"""
from __future__ import annotations

import json
import statistics as st
import time
import urllib.request
from datetime import datetime, timezone

from bots.copy.config import get_copy_settings

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
_KEY = get_copy_settings().birdeye_api_key
RATE_SLEEP = 1.1
SLIP = 0.015                 # 150 bps each way
LADDER = [4.0, 10.0, 50.0, 1000.0]
LADDER_FRAC = 0.25
TRAIL_ACT = 1.20             # activate trailing at +20%
TRAIL_KEEP = 0.55            # exit remaining at peak*0.55 (45% multiplicative trail)
WINDOW_H = 96                # fetch horizon
STAKE = 400.0                # $ per trade, for net-$ comparability

# 62 deduped first-promo events on the 98-runner corpus (2026-07-11).
# t=token, s=symbol, rd=run_date, p=promo payment ms, band=adjacent|stale.
EVENTS = json.loads(r'''
[{"t":"J8PSdNP3QewKq2Z1JJJFDMaqF7KcaiJhR7gbr5KZpump","s":"TripleT","rd":"2026-05-19","p":1771922308499,"band":"stale"},{"t":"2fUFhZyd47Mapv9wcfXh5gnQwFXtqcYu9xAN4THBpump","s":"RNT","rd":"2026-05-22","p":1727829436473,"band":"stale"},{"t":"4Cnk9EPnW5ixfLZatCPJjDB1PUtcRpVVgTQukm9epump","s":"DADDY","rd":"2026-05-22","p":1727829458832,"band":"stale"},{"t":"8G5ayEsJF4Q7FEWEGeF4jtnUWZBEKCqhySTFQf9Ppump","s":"HeavyPulp","rd":"2026-05-23","p":1769521836907,"band":"stale"},{"t":"DiiTPZdpd9t3XorHiuZUu4E1FoSaQ7uGN4q9YkQupump","s":"ELIZABETH","rd":"2026-05-23","p":1757617345899,"band":"stale"},{"t":"72FkeF1cpBMtbordhTVNVbBGdaN5DfHcchstHwPWpump","s":"PUMPVILLE","rd":"2026-05-25","p":1762980060196,"band":"stale"},{"t":"8ncucXv6U6epZKHPbgaEBcEK399TpHGKCquSt4RnmX4f","s":"TRENCHER","rd":"2026-05-25","p":1745487695944,"band":"stale"},{"t":"4D8qUHm334fxqeTauPvF8gQ7fYgrD4Mpmb1Wy6ftUSWR","s":"USWR","rd":"2026-05-27","p":1779393897736,"band":"stale"},{"t":"FeMbDoX7R1Psc4GEcvJdsbNbZA3bfztcyDCatJVJpump","s":"three","rd":"2026-05-31","p":1777443464630,"band":"stale"},{"t":"24Kko3VWwMKtkB9CVct2ZAZgJzb2R2iNrYxCpTYFpump","s":"APU","rd":"2026-06-05","p":1780667130986,"band":"adjacent"},{"t":"Tqj8yFmagrg7oorpQkVGYR52r96RFTamvWfth9bpump","s":"KINS","rd":"2026-06-06","p":1779472292211,"band":"stale"},{"t":"BcHEaaTCvycPwwsJ9yQTXdHP9X2gCLkznDbZ8VySpump","s":"Jotchua","rd":"2026-06-07","p":1780807770983,"band":"adjacent"},{"t":"B6f27ETGcjgGNB1fqULJbXVmw9FnL8HgBp7R83hmpump","s":"drooling","rd":"2026-06-08","p":1780040829402,"band":"stale"},{"t":"h5NciPdMZ5QCB5BYETJMYBMpVx9ZuitR6HcVjyBhood","s":"HOOD","rd":"2026-06-10","p":1738286945292,"band":"stale"},{"t":"4SnKwnz6DyagftnFqdxsvWvehrcbEDhxmmXNQk2Jpump","s":"Joby","rd":"2026-06-16","p":1781218380857,"band":"stale"},{"t":"EmcxFTNVDqyLHp11NvwvLZ4D7LKGbG9i7B8RF7dwpump","s":"ZERO","rd":"2026-06-17","p":1780602824526,"band":"stale"},{"t":"8wxkvAfEns76yBzu4MnbV7VnXWjg3iDPA9uwAQ6cpump","s":"SOLANGELES","rd":"2026-06-18","p":1779397450751,"band":"stale"},{"t":"5pYB12kEhfhSFXJjZ7JtyqDpt6uUqhsF6iu6Ee9spump","s":"CATWIF","rd":"2026-06-22","p":1782112590129,"band":"adjacent"},{"t":"FMqh9mqR6drPZqqW6wPqLHxX4rqNDWGhYLaMfoaJpump","s":"world","rd":"2026-06-22","p":1782237702263,"band":"adjacent"},{"t":"3d1qHSAkQhoN7kN1C6tvpAArCkXWxwYdBng6taXCDM6u","s":"RTM","rd":"2026-06-26","p":1758025482984,"band":"stale"},{"t":"9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump","s":"ANSEM","rd":"2026-06-27","p":1781644417822,"band":"stale"},{"t":"FDEF2U9geiWS7sdGPCUo2r1wGswkJr2mmVTECiH6pump","s":"MITCH","rd":"2026-06-27","p":1756850595182,"band":"stale"},{"t":"BDdzUjksj1J4bSnMveQ5tV9Up8A9c6YS1tHrxNw3pump","s":"FOMO","rd":"2026-06-28","p":1782671821456,"band":"adjacent"},{"t":"CuGJf6cfDfMh4UxVgNJ5KFQ6v8Wv3qrqop6cFKsGpump","s":"TATE","rd":"2026-06-28","p":1782676447775,"band":"adjacent"},{"t":"HULaBKR1eK3SA18FpfEav8Z2t7TJ1qEC775YHTLNpump","s":"DAVID","rd":"2026-06-28","p":1782684620027,"band":"adjacent"},{"t":"JCKwsT8UAbygnFkZ7u3amDUM7BXRtwUhCsHQv2khpump","s":"CHANCE","rd":"2026-06-28","p":1781059611994,"band":"stale"},{"t":"ERPtViqckt1zqpS2abUx67CgLcP54CkhBPYz3yRUpump","s":"yep","rd":"2026-06-29","p":1782735279996,"band":"adjacent"},{"t":"2aQKC1xiKpEyAWvzSDrULnYLVuybWyx7qrsnpP8hpump","s":"Udin","rd":"2026-06-29","p":1782800690757,"band":"adjacent"},{"t":"DdPrHYqM8Ueovnk9kAnAgoGhswkuaTqmxcoZzU3Zpump","s":"manlet","rd":"2026-07-01","p":1782774171115,"band":"adjacent"},{"t":"Bqs76KADGDkiwskqdqTu1jaNrpJyzV5ZxpvJrL5npump","s":"Hobbes","rd":"2026-07-01","p":1782588968375,"band":"stale"},{"t":"6hpfrxQb2kn9iprcJcZ6uXruGAwDe12W9zT4y4WSpump","s":"Goofreck","rd":"2026-07-01","p":1747862354629,"band":"stale"},{"t":"4N4DnNo3qpPks9aQCkcWkzoir8tnvT6diS4TnnZibonk","s":"KITTY","rd":"2026-07-01","p":1759430705312,"band":"stale"},{"t":"HmTi3CQfKfXWbn1tNoiAxH7GzMV7L3tDAmPWabZEBAGS","s":"P0","rd":"2026-07-01","p":1772829525434,"band":"stale"},{"t":"F5hqdbykXuksp8P78CAZenSvpPShAYKrP2U2MiZMdgFN","s":"bop","rd":"2026-07-01","p":1729720242979,"band":"stale"},{"t":"48oT2QgpyPFUiJYXbvZTS1tVDp98R9uYAJX5ojNrpump","s":"SEMAN","rd":"2026-07-02","p":1783065136874,"band":"adjacent"},{"t":"CFPkPq1eYPR8GLzEo59wUbbMioX4bshaTQiSGzTSpump","s":"MENSA","rd":"2026-07-02","p":1782959300419,"band":"adjacent"},{"t":"5RyeWfbjVw6Ktj6j8SmTiAnVXWvmV8YxGtTebNsU2dBo","s":"Pauly","rd":"2026-07-02","p":1727828227892,"band":"stale"},{"t":"2jz9E5JrEbxLg1RhU68aaSikDvpQurCEZz9BBF9rpump","s":"PATTYICE","rd":"2026-07-02","p":1782835819725,"band":"adjacent"},{"t":"4MrsXQzaosYNyFd4wKDvgnC5xRtRqgXRrijFTGj9pump","s":"NORMIE","rd":"2026-07-03","p":1782996934495,"band":"adjacent"},{"t":"96X4zg5T4NFWzTVFXHsadvYbxbzFX2Rqt3GXUv92pump","s":"Balloon","rd":"2026-07-03","p":1783082391619,"band":"adjacent"},{"t":"4Vn4cyu67VJouC3BqEu7AAQix8k7ryLrA6mMmkjjpump","s":"Z","rd":"2026-07-03","p":1783167252141,"band":"adjacent"},{"t":"42cXQvAAr7hcPBPWAS4ocVtDyeJ4Fa6gRR2uG4gppump","s":"TBB","rd":"2026-07-03","p":1782832099290,"band":"adjacent"},{"t":"ESAYabnPzfaZGcwNtTL5RL1RBaoW7vYUNx5n6QfTpump","s":"Pigeon","rd":"2026-07-03","p":1782848473739,"band":"adjacent"},{"t":"8oQEW25mcVzeWvcjkGup9ob3KafVMNdzAi8XXmmWpump","s":"Fro","rd":"2026-07-04","p":1758500396411,"band":"stale"},{"t":"GbpARMvdpDpavgLJXzTSgWAcSta2FeEYfjAZZ91upump","s":"MURAD","rd":"2026-07-04","p":1783272816965,"band":"adjacent"},{"t":"CvqxZzFZqDKSpD1bNSkwQ6j9stSCBVgQ9GT4tLdKpump","s":"TOLY","rd":"2026-07-04","p":1783201439281,"band":"adjacent"},{"t":"EYvkcWB5S4DFv7gJqR8D2W76U9n7kD3jvYcbSXdppump","s":"HAALAND","rd":"2026-07-04","p":1782377388918,"band":"stale"},{"t":"4PRz3EwhbjrrX6YksMDuUzrXT51pr7CQtXNCravhpump","s":"ACM","rd":"2026-07-04","p":1783230637145,"band":"adjacent"},{"t":"7thY5PzYdH311NXYE6mecBjUs7wcosk1bWw2qfH4pump","s":"Sapijiju","rd":"2026-07-05","p":1764213536398,"band":"stale"},{"t":"6baGyq4HLbUn93MQUGFqBktpXP8BRjpoxSsAap4ppump","s":"LEVI","rd":"2026-07-05","p":1783309186452,"band":"adjacent"},{"t":"6UGNg119LV6YjSCQ5K6Z6jsLLLetpMJ1vPdhiU8qpump","s":"CURB","rd":"2026-07-05","p":1783261474959,"band":"adjacent"},{"t":"E2ueKQ3EDTTmCkUA17j3KeTb2u6VT91xiyECdKRzpump","s":"tolywifhat","rd":"2026-07-06","p":1783320989756,"band":"adjacent"},{"t":"4eKYoR1hBHnRaYyg2d57H36uUatRNyZr1NRP9ScNpump","s":"DONALT","rd":"2026-07-06","p":1783312206854,"band":"adjacent"},{"t":"6NwarBvDkXhByqVp2Qkq5i9XbtA2B3Bwe8SWGu9vpump","s":"Cupsey","rd":"2026-07-06","p":1768936361921,"band":"stale"},{"t":"CizcDa22HJjXybKLriibqP167GcJjqiDHDPtuiMmpump","s":"ok","rd":"2026-07-06","p":1783037824685,"band":"adjacent"},{"t":"6xCtR2Eq1VumsoRdNutcfSQfLMk7xUa2BrMx18tqpump","s":"DEXBULL","rd":"2026-07-07","p":1783513897918,"band":"adjacent"},{"t":"22deNCri9bBae1GWZxdaDxipMWdwAUQ2ddvxdSmgpump","s":"CASHCAT","rd":"2026-07-07","p":1783483672911,"band":"adjacent"},{"t":"9y6hXFBFN1fJGfAAYgkXVVfQATG1mEJsmTrCXcT6pump","s":"Silk","rd":"2026-07-08","p":1783512120407,"band":"adjacent"},{"t":"4ko5tSr5o3H4v1sFtjTSd9MPUW7yx5AFCpkNPoL6pump","s":"febu","rd":"2026-07-08","p":1783548435566,"band":"adjacent"},{"t":"J5ZognFJEepsWsCQPmqVchvEvsUMomEkxd8LbMrH2J7","s":"Bullscan","rd":"2026-07-08","p":1783535255199,"band":"adjacent"},{"t":"BtUiqGdsW3H47yvtyKSLhQfzVpwov3wyYY6qssU9pump","s":"reptilecoin","rd":"2026-07-09","p":1783639996882,"band":"adjacent"},{"t":"CgnQ8a1u99Gk7qoySMd2wrGPMPm9bdcSJNhqMEkYpump","s":"Loom","rd":"2026-07-09","p":1783474144346,"band":"adjacent"}]
''')

CONFIGS = [
    {"name": "stop8_t12h", "stop": 0.08, "timeout_h": 12},
    {"name": "stop8_t72h", "stop": 0.08, "timeout_h": 72},
    {"name": "stop30_t12h", "stop": 0.30, "timeout_h": 12},
    {"name": "stop30_t72h", "stop": 0.30, "timeout_h": 72},
]


def be(path: str, tries: int = 3):
    for i in range(tries):
        try:
            r = urllib.request.Request(
                "https://public-api.birdeye.so" + path,
                headers={"X-API-KEY": _KEY, "x-chain": "solana",
                         "Accept": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(r, timeout=25) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (i + 1))
                continue
            return None
        except Exception:
            time.sleep(2)
    return None


def fetch_series(mint: str, promo_ms: int):
    t_from = int(promo_ms / 1000) - 3600
    t_to = t_from + WINDOW_H * 3600 + 3600
    h = be(f"/defi/history_price?address={mint}&address_type=token"
           f"&type=1H&time_from={t_from}&time_to={t_to}")
    items = ((h or {}).get("data") or {}).get("items") or []
    return sorted((it["unixTime"], float(it["value"]))
                  for it in items if it.get("value"))


def simulate(pts, promo_s: float, stop: float, timeout_h: int):
    """Returns (ret, exit_reason, mfe) or None if no entry point."""
    series = [p for p in pts if p[0] >= promo_s]
    if len(series) < 2:
        return None
    entry_ts, entry_raw = series[0]
    entry_px = entry_raw * (1 + SLIP)
    remaining = 1.0
    proceeds = 0.0
    peak = entry_raw
    activated = False
    hit = set()
    mfe = 1.0
    reason = "end_of_data"
    exit_px = series[-1][1]

    for ts, px in series[1:]:
        mfe = max(mfe, px / entry_raw)
        peak = max(peak, px)
        if ts - entry_ts >= timeout_h * 3600:
            reason, exit_px = "timeout", px
            break
        if px <= entry_px * (1 - stop):
            reason, exit_px = "hard_stop", px
            break
        if not activated and px >= entry_px * TRAIL_ACT:
            activated = True
        if activated and px <= peak * TRAIL_KEEP:
            reason, exit_px = "trailing", px
            break
        for lv in LADDER:
            if lv not in hit and px >= lv * entry_px:
                hit.add(lv)
                frac = min(LADDER_FRAC, remaining)
                proceeds += frac * px * (1 - SLIP)
                remaining -= frac
        if remaining <= 1e-9:
            reason, exit_px = "ladder_out", px
            break

    proceeds += remaining * exit_px * (1 - SLIP)
    ret = proceeds / entry_px - 1.0
    return ret, reason, mfe


def main():
    series_cache = {}
    print(f"fetching 1H series for {len(EVENTS)} promo events...")
    for i, e in enumerate(EVENTS):
        series_cache[(e["t"], e["p"])] = fetch_series(e["t"], e["p"])
        time.sleep(RATE_SLEEP)
        if (i + 1) % 15 == 0:
            print(f"  {i + 1}/{len(EVENTS)}")

    for cfg in CONFIGS:
        print(f"\n================ config {cfg['name']} "
              f"(stop {cfg['stop']:.0%}, timeout {cfg['timeout_h']}h) ================")
        for band in ("adjacent", "stale"):
            rets, reasons, mfes, skipped = [], {}, [], 0
            for e in EVENTS:
                if e["band"] != band:
                    continue
                pts = series_cache[(e["t"], e["p"])]
                out = simulate(pts, e["p"] / 1000.0, cfg["stop"], cfg["timeout_h"])
                if out is None:
                    skipped += 1
                    continue
                ret, reason, mfe = out
                rets.append(ret)
                mfes.append(mfe)
                reasons[reason] = reasons.get(reason, 0) + 1
            if not rets:
                print(f"  [{band}] no simulatable events (skipped {skipped})")
                continue
            wins = sum(1 for r in rets if r > 0)
            net = sum(r * STAKE for r in rets)
            print(f"  [{band}] n={len(rets)} (skipped {skipped})  "
                  f"WR {wins}/{len(rets)} ({100*wins/len(rets):.0f}%)")
            print(f"    ret: median {st.median(rets):+.1%}  mean {st.mean(rets):+.1%}  "
                  f"best {max(rets):+.1%}  worst {min(rets):+.1%}")
            print(f"    net on ${STAKE:.0f} stakes: ${net:+,.0f}   "
                  f"exit reasons: {reasons}")
            print(f"    MFE (96h): median {st.median(mfes):.2f}x  "
                  f">=2x: {sum(1 for m in mfes if m >= 2)}/{len(mfes)}  "
                  f">=4x: {sum(1 for m in mfes if m >= 4)}/{len(mfes)}")


if __name__ == "__main__":
    main()
