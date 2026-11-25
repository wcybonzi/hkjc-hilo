import argparse
import asyncio
import json
import os
import subprocess
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright

HKT = timezone(timedelta(hours=8))
OUT_PATH = os.path.join("data", "hkjc_hilo.json")

LIVE_OR_DONE_STATUSES = {
    "INPLAY", "LIVE", "STARTED", "FIRSTHALF", "HALFTIME", "SECONDHALF",
    "COMPLETED", "FINISHED", "ENDED", "FINAL", "RESULT", "CLOSED",
    "CANCELLED", "POSTPONED", "ABANDONED", "VOID", "SUSPENDED"
}

PREFERRED_LINES = ("2.5", "2.0/2.5", "2.5/3.0", "2.0", "3.0", "3.0/3.5", "1.5/2.0", "3.5")
LINE_PRIORITY = {ln: idx for idx, ln in enumerate(PREFERRED_LINES)}


def line_rank(line_str):
    return LINE_PRIORITY.get(str(line_str or "").strip(), 999)


def load_previous_memory():
    if not os.path.exists(OUT_PATH):
        return {}
    try:
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        items = payload.get("matches", []) if isinstance(payload, dict) else payload
        mem = {}
        for m in items:
            mid = str(m.get("id") or m.get("code") or "").strip()
            if mid:
                mem[mid] = m
        return mem
    except Exception:
        return {}


def normalize_line(raw_line):
    s = str(raw_line or "").strip().replace(" ", "")
    return s.replace("[", "").replace("]", "")


def parse_odd_float(raw_val):
    if raw_val is None:
        return 0.0
    s = str(raw_val).strip().replace("@", "").replace(",", "")
    try:
        return float(s)
    except Exception:
        return 0.0


def parse_iso_to_ms(val):
    if not val:
        return 0
    s = str(val).strip()
    if s.isdigit():
        num = int(s)
        return num if num > 10_000_000_000 else num * 1000
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=HKT)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def extract_team_names(node, side_key):
    t = node.get(side_key) or {}
    if isinstance(t, dict):
        ch = t.get("name_ch") or t.get("nameCh") or t.get("chName") or t.get("name") or ""
        en = t.get("name_en") or t.get("nameEn") or t.get("enName") or ""
        return str(ch).strip(), str(en).strip()
    return str(t).strip(), ""


def extract_pools_from_match(node):
    best_hilo = None
    had = {"H": 0.0, "D": 0.0, "A": 0.0}

    pools = node.get("foPools") or node.get("pools") or node.get("oddsPools") or []
    hilo_candidates = {}

    for pool in pools:
        if not isinstance(pool, dict):
            continue
        ptype = str(pool.get("oddsType") or pool.get("poolType") or pool.get("type") or "").upper()
        lines = pool.get("lines") or [pool]

        if ptype == "HAD":
            for ln in lines:
                for comb in (ln.get("combinations") or ln.get("selections") or []):
                    sel = str(comb.get("str") or comb.get("selection") or comb.get("code") or "").upper()
                    odd = parse_odd_float(comb.get("currentOdds") or comb.get("odds"))
                    if sel in ("H", "HOME", "1") and odd > 1.0:
                        had["H"] = odd
                    elif sel in ("D", "DRAW", "X") and odd > 1.0:
                        had["D"] = odd
                    elif sel in ("A", "AWAY", "2") and odd > 1.0:
                        had["A"] = odd

        if ptype in ("HIL", "HILO", "MHL"):
            for ln in lines:
                status_str = str(ln.get("status") or pool.get("status") or "AVAILABLE").upper()
                if status_str in ("UNAVAILABLE", "SUSPENDED", "CLOSED"):
                    continue
                cond = normalize_line(ln.get("condition") or ln.get("line") or pool.get("condition") or "")
                high_odd, low_odd = 0.0, 0.0
                for comb in (ln.get("combinations") or ln.get("selections") or []):
                    c_cond = normalize_line(comb.get("line") or comb.get("condition") or cond)
                    if c_cond:
                        cond = c_cond
                    sel = str(comb.get("str") or comb.get("selection") or comb.get("name_ch") or "").upper()
                    odd = parse_odd_float(comb.get("currentOdds") or comb.get("odds"))
                    if sel in ("H", "HIGH", "OVER", "大") and odd > 1.01:
                        high_odd = odd
                    elif sel in ("L", "LOW", "UNDER", "細") and odd > 1.01:
                        low_odd = odd
                if cond and 1.14 <= high_odd <= 5.50 and 1.14 <= low_odd <= 5.50:
                    hilo_candidates[cond] = (high_odd, low_odd)

    for pref in PREFERRED_LINES:
        if pref in hilo_candidates:
            h_val, l_val = hilo_candidates[pref]
            best_hilo = (pref, h_val, l_val)
            break

    if not best_hilo and hilo_candidates:
        first_line = next(iter(hilo_candidates.keys()))
        h_val, l_val = hilo_candidates[first_line]
        best_hilo = (first_line, h_val, l_val)

    return best_hilo, had


def walk_graphql_matches(obj, collected, now_ms):
    if isinstance(obj, list):
        for item in obj:
            walk_graphql_matches(item, collected, now_ms)
        return
    if not isinstance(obj, dict):
        return

    front_id = str(obj.get("frontEndId") or obj.get("matchId") or obj.get("code") or "").strip()
    if front_id.upper().startswith("FB") and (obj.get("homeTeam") or obj.get("foPools") or obj.get("pools")):
        status_raw = str(obj.get("status") or obj.get("matchStatus") or "").upper()
        if status_raw in LIVE_OR_DONE_STATUSES:
            collected.pop(front_id, None)
            return

        ko_ms = (
            parse_iso_to_ms(obj.get("kickOffTime"))
            or parse_iso_to_ms(obj.get("matchTime"))
            or parse_iso_to_ms(obj.get("matchDate"))
        )
        if ko_ms > 0 and ko_ms < (now_ms - 60_000):
            collected.pop(front_id, None)
            return

        home_ch, home_en = extract_team_names(obj, "homeTeam")
        away_ch, away_en = extract_team_names(obj, "awayTeam")
        tourn = obj.get("tournament") or {}
        league = (
            tourn.get("name_ch") or tourn.get("nameCh") or tourn.get("shortName_ch") or "足球賽事"
            if isinstance(tourn, dict) else str(tourn or "足球賽事")
        )

        best_hilo, had = extract_pools_from_match(obj)
        existing = collected.get(front_id)

        # Case A: Packet has HAD odds only (e.g. from /ch/football/home) -> enrich HAD without overwriting HIL!
        if existing and not best_hilo:
            if had["H"] > 1.0:
                existing["hadHome"] = round(had["H"], 2)
            if had["D"] > 1.0:
                existing["hadDraw"] = round(had["D"], 2)
            if had["A"] > 1.0:
                existing["hadAway"] = round(had["A"], 2)
            return

        # Case B: Packet has live HIL odds -> update whenever line is higher/equal priority!
        if home_ch and away_ch and best_hilo:
            line_str, h_odd, l_odd = best_hilo
            if not existing:
                collected[front_id] = {
                    "id": front_id,
                    "league": str(league).strip(),
                    "home": home_ch,
                    "away": away_ch,
                    "homeEn": home_en,
                    "awayEn": away_en,
                    "kickoffMs": ko_ms,
                    "line": line_str,
                    "oddsHigh": round(h_odd, 2),
                    "oddsLow": round(l_odd, 2),
                    "hadHome": round(had["H"], 2) if had["H"] > 1 else 0,
                    "hadDraw": round(had["D"], 2) if had["D"] > 1 else 0,
                    "hadAway": round(had["A"], 2) if had["A"] > 1 else 0,
                }
            else:
                # Never overwrite a pure 2.5 line with an inferior secondary line from /home
                if line_rank(line_str) <= line_rank(existing["line"]):
                    existing["line"] = line_str
                    existing["oddsHigh"] = round(h_odd, 2)
                    existing["oddsLow"] = round(l_odd, 2)
                if home_en and not existing.get("homeEn"):
                    existing["homeEn"] = home_en
                if away_en and not existing.get("awayEn"):
                    existing["awayEn"] = away_en
                if ko_ms > 0:
                    existing["kickoffMs"] = ko_ms
                if had["H"] > 1.0:
                    existing["hadHome"] = round(had["H"], 2)
                if had["D"] > 1.0:
                    existing["hadDraw"] = round(had["D"], 2)
                if had["A"] > 1.0:
                    existing["hadAway"] = round(had["A"], 2)
            return

    for v in obj.values():
        if isinstance(v, (dict, list)):
            walk_graphql_matches(v, collected, now_ms)


async def run_single_scrape_cycle(browser):
    prev_mem = load_previous_memory()
    now_dt = datetime.now(HKT)
    now_ms = int(now_dt.timestamp() * 1000)
    now_label = now_dt.strftime("%d/%m %H:%M")

    collected = {}
    captured_gql_requests = []

    context = await browser.new_context(
        locale="zh-HK",
        timezone_id="Asia/Hong_Kong",
        viewport={"width": 1440, "height": 2400},
        extra_http_headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        },
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    page = await context.new_page()

    async def handle_response(resp):
        url = resp.url.lower()
        if "graphql" not in url and "info.cld.hkjc.com" not in url:
            return
        try:
            req = resp.request
            if req.method == "POST" and req.post_data:
                captured_gql_requests.append((resp.url, req.post_data))
            payload = await resp.json()
            walk_graphql_matches(payload, collected, now_ms)
        except Exception:
            pass

    page.on("response", handle_response)

    # Visit /home FIRST for HAD odds, then /hil LAST so latest live HIL 2.5 odds always win!
    cb_ts = int(now_ms)
    for target_url in (
        f"https://bet.hkjc.com/ch/football/home?_={cb_ts}",
        f"https://bet.hkjc.com/ch/football/hil?_={cb_ts + 1}"
    ):
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(4200)
            for _ in range(26):
                await page.mouse.wheel(0, 1450)
                await page.wait_for_timeout(360)
        except Exception as e:
            print(f"Navigation note ({target_url}): {e}")

    # Replay intercepted GraphQL queries across paginated windows with cache: 'no-store'
    for gql_url, post_raw in list(captured_gql_requests):
        try:
            body = json.loads(post_raw)
            if not isinstance(body, dict) or "variables" not in body:
                continue
            vars_dict = body.get("variables") or {}
            if "startIndex" in vars_dict or "endIndex" in vars_dict:
                for start_idx in (1, 51, 101, 151, 201):
                    patched = json.loads(post_raw)
                    patched["variables"]["startIndex"] = start_idx
                    patched["variables"]["endIndex"] = start_idx + 99
                    res_json = await page.evaluate(
                        """async ({url, bodyStr}) => {
                            const r = await fetch(url, {
                                method: 'POST',
                                cache: 'no-store',
                                headers: {
                                    'content-type': 'application/json',
                                    'cache-control': 'no-cache, no-store'
                                },
                                body: bodyStr
                            });
                            return r.ok ? await r.json() : null;
                        }""",
                        {"url": gql_url, "bodyStr": json.dumps(patched)}
                    )
                    if res_json:
                        walk_graphql_matches(res_json, collected, now_ms)
        except Exception:
            pass

    await context.close()

    final_matches = []
    changed_count = 0

    for mid, cur in collected.items():
        if cur["kickoffMs"] > 0 and cur["kickoffMs"] < (now_ms - 60_000):
            continue

        old = prev_mem.get(mid, {})
        same_line = str(old.get("line", cur["line"])) == cur["line"]

        open_h = float(old.get("openHigh") or cur["oddsHigh"]) if same_line else cur["oddsHigh"]
        open_l = float(old.get("openLow") or cur["oddsLow"]) if same_line else cur["oddsLow"]
        last_h = float(old.get("oddsHigh") or cur["oddsHigh"]) if same_line else cur["oddsHigh"]
        last_l = float(old.get("oddsLow") or cur["oddsLow"]) if same_line else cur["oddsLow"]

        history = list(old.get("oddsHistory") or []) if same_line else []
        if not history:
            history.append({"t": now_label + " (初盤)", "h": round(open_h, 2), "l": round(open_l, 2)})

        moved = abs(cur["oddsHigh"] - last_h) >= 0.01 or abs(cur["oddsLow"] - last_l) >= 0.01
        if moved:
            changed_count += 1
            history.append({"t": now_label, "h": cur["oddsHigh"], "l": cur["oddsLow"]})
            history = history[-14:]

        prev_h = last_h if moved else float(old.get("prevHigh") or open_h)
        prev_l = last_l if moved else float(old.get("prevLow") or open_l)

        drop_h = round(((open_h - cur["oddsHigh"]) / open_h) * 100, 2) if open_h > 1.01 else 0.0
        drop_l = round(((open_l - cur["oddsLow"]) / open_l) * 100, 2) if open_l > 1.01 else 0.0

        cur.update({
            "openHigh": round(open_h, 2),
            "openLow": round(open_l, 2),
            "prevHigh": round(prev_h, 2),
            "prevLow": round(prev_l, 2),
            "dropPctHigh": drop_h,
            "dropPctLow": drop_l,
            "oddsHistory": history,
            "updatedAtHkt": now_label
        })
        final_matches.append(cur)

    final_matches.sort(key=lambda x: (x["kickoffMs"] if x["kickoffMs"] > 0 else 9_999_999_999_999, x["id"]))

    os.makedirs("data", exist_ok=True)
    if len(final_matches) == 0 and prev_mem:
        print("Warning: 0 matches scraped on this cycle; pruning started matches from previous cache.")
        final_matches = [
            m for m in prev_mem.values()
            if not m.get("kickoffMs") or int(m.get("kickoffMs", 0)) > now_ms
        ]

    # Check if match list or any odds changed vs previous file
    prev_ids = set(prev_mem.keys())
    cur_ids = {m["id"] for m in final_matches}
    list_changed = prev_ids != cur_ids or changed_count > 0 or not os.path.exists(OUT_PATH)

    output_payload = {
        "version": "v19",
        "updatedAtIso": now_dt.isoformat(),
        "updatedAtHkt": now_dt.strftime("%d/%m (%a) %H:%M:%S HKT"),
        "matchCount": len(final_matches),
        "oddsChangedCount": changed_count,
        "matches": final_matches
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, ensure_ascii=False, indent=2)

    print(f"[{now_label} HKT] Scraped {len(final_matches)} matches ({changed_count} odds changed).")
    return list_changed, changed_count


def git_commit_and_push_if_needed(push_enabled):
    if not push_enabled:
        return
    try:
        subprocess.run(["git", "config", "user.name", "hkjc-sync-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "actions@github.com"], check=False)
        subprocess.run(["git", "add", OUT_PATH], check=False)
        diff_res = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if diff_res.returncode == 0:
            print("Git: No file changes to push.")
            return
        stamp = datetime.now(HKT).strftime("%H:%M:%S HKT")
        subprocess.run(["git", "commit", "-m", f"Live HKJC odds sync ({stamp})"], check=False)
        subprocess.run(["git", "pull", "--rebase", "origin", "HEAD"], check=False)
        subprocess.run(["git", "push", "origin", "HEAD"], check=False)
        print(f"Git: Pushed fresh HKJC odds at {stamp}.")
    except Exception as e:
        print(f"Git push note: {e}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=1, help="Number of scrape cycles in this job")
    parser.add_argument("--interval", type=int, default=180, help="Seconds between cycles")
    parser.add_argument("--git-push", action="store_true", help="Commit and push immediately after each cycle")
    args = parser.parse_args()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        for idx in range(args.cycles):
            print(f"--- Starting HKJC Live Odds Cycle {idx + 1}/{args.cycles} ---")
            changed, _ = await run_single_scrape_cycle(browser)
            if changed or idx == 0:
                git_commit_and_push_if_needed(args.git_push)
            if idx + 1 < args.cycles:
                await asyncio.sleep(args.interval)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
