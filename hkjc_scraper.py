import asyncio
import copy
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright

HKT = timezone(timedelta(hours=8))
OUT_PATH = os.path.join("data", "hkjc_hilo.json")

LIVE_OR_DONE_STATUSES = {
    "INPLAY", "LIVE", "STARTED", "FIRSTHALF", "HALFTIME", "SECONDHALF",
    "COMPLETED", "FINISHED", "ENDED", "FINAL", "RESULT", "CLOSED",
    "CANCELLED", "POSTPONED", "ABANDONED", "VOID", "SUSPENDED"
}

PREFERRED_LINES = ("2.5", "2.0/2.5", "2.5/3.0", "2.0", "3.0", "3.0/3.5", "1.5/2.0", "3.5", "1.5", "4.0")
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
    return s.replace(chr(91), "").replace(chr(93), "")


def parse_odd_float(raw_val):
    if raw_val is None:
        return 0.0
    s = str(raw_val).strip().replace(",", "")
    if "@" in s:
        s = s.split("@")[-1]
    try:
        return float(s)
    except Exception:
        return 0.0


def parse_kickoff_to_ms(val):
    """Parses ONLY real kick-off clock timestamps; ignores date-only midnight strings."""
    if not val:
        return 0
    s = str(val).strip()
    if s.isdigit():
        num = int(s)
        return num if num > 10_000_000_000 else num * 1000
    # Ignore pure YYYY-MM-DD without clock time so today's evening matches never look past midnight
    if len(s) <= 10 or ("T" not in s and ":" not in s):
        return 0
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
                    if sel in ("H", "HOME", "1", "主") and odd > 1.0:
                        had["H"] = odd
                    elif sel in ("D", "DRAW", "X", "和") and odd > 1.0:
                        had["D"] = odd
                    elif sel in ("A", "AWAY", "2", "客") and odd > 1.0:
                        had["A"] = odd

        if ptype in ("HIL", "HILO", "MHL"):
            for ln in lines:
                status_str = str(ln.get("status") or pool.get("status") or "AVAILABLE").upper()
                if status_str in ("UNAVAILABLE", "SUSPENDED", "CLOSED", "REFUND"):
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
                if cond and 1.06 <= high_odd <= 9.50 and 1.06 <= low_odd <= 9.50:
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


def upsert_match_record(collected, front_id, league, home_ch, away_ch, home_en, away_en, ko_ms, best_hilo, had):
    existing = collected.get(front_id)
    if existing and not best_hilo:
        if had["H"] > 1.0:
            existing["hadHome"] = round(had["H"], 2)
        if had["D"] > 1.0:
            existing["hadDraw"] = round(had["D"], 2)
        if had["A"] > 1.0:
            existing["hadAway"] = round(had["A"], 2)
        if ko_ms > 0 and not existing.get("kickoffMs"):
            existing["kickoffMs"] = ko_ms
        return

    if not (home_ch and away_ch and best_hilo):
        return

    line_str, h_odd, l_odd = best_hilo
    if not existing:
        collected[front_id] = {
            "id": front_id,
            "league": str(league or "足球賽事").strip(),
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
        if line_rank(line_str) <= line_rank(existing["line"]):
            existing["line"] = line_str
            existing["oddsHigh"] = round(h_odd, 2)
            existing["oddsLow"] = round(l_odd, 2)
        if home_en and not existing.get("homeEn"):
            existing["homeEn"] = home_en
        if away_en and not existing.get("awayEn"):
            existing["awayEn"] = away_en
        if league and existing.get("league") in ("", "足球賽事"):
            existing["league"] = str(league).strip()
        if ko_ms > 0:
            existing["kickoffMs"] = ko_ms
        if had["H"] > 1.0:
            existing["hadHome"] = round(had["H"], 2)
        if had["D"] > 1.0:
            existing["hadDraw"] = round(had["D"], 2)
        if had["A"] > 1.0:
            existing["hadAway"] = round(had["A"], 2)


def walk_graphql_matches(obj, collected, now_ms):
    if isinstance(obj, list):
        for item in obj:
            walk_graphql_matches(item, collected, now_ms)
        return
    if not isinstance(obj, dict):
        return

    raw_id = str(obj.get("frontEndId") or obj.get("matchId") or obj.get("code") or "").strip()
    has_teams = bool(obj.get("homeTeam") and obj.get("awayTeam"))
    has_pools = bool(obj.get("foPools") or obj.get("pools") or obj.get("oddsPools"))

    if (raw_id.upper().startswith("FB") or (raw_id and has_teams)) and (has_teams or has_pools):
        front_id = raw_id.upper() if raw_id.upper().startswith("FB") else f"FB{raw_id}"
        status_raw = str(obj.get("status") or obj.get("matchStatus") or "").upper()
        if status_raw in LIVE_OR_DONE_STATUSES:
            collected.pop(front_id, None)
            return

        # Strictly parse clock-based kick-off fields ONLY (never date-only matchDate!)
        ko_ms = (
            parse_kickoff_to_ms(obj.get("kickOffTime"))
            or parse_kickoff_to_ms(obj.get("matchTime"))
            or parse_kickoff_to_ms(obj.get("esst"))
        )
        if ko_ms > 0 and ko_ms < (now_ms - 120_000):
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
        upsert_match_record(collected, front_id, league, home_ch, away_ch, home_en, away_en, ko_ms, best_hilo, had)
        return

    for v in obj.values():
        if isinstance(v, (dict, list)):
            walk_graphql_matches(v, collected, now_ms)


def patch_pagination_vars(node, start_idx, end_idx):
    """Recursively patches startIndex / endIndex inside single or batched GraphQL bodies."""
    mutated = False
    if isinstance(node, list):
        for elem in node:
            if patch_pagination_vars(elem, start_idx, end_idx):
                mutated = True
    elif isinstance(node, dict):
        if "startIndex" in node or "endIndex" in node:
            node["startIndex"] = start_idx
            node["endIndex"] = end_idx
            mutated = True
        for val in node.values():
            if isinstance(val, (dict, list)) and patch_pagination_vars(val, start_idx, end_idx):
                mutated = True
    return mutated


async def scroll_all_containers(page, steps=24):
    await page.mouse.move(720, 520)
    for _ in range(steps):
        await page.mouse.wheel(0, 1700)
        await page.evaluate(
            """() => {
                window.scrollBy(0, 1700);
                const nodes = document.querySelectorAll('div, main, section');
                for (const el of nodes) {
                    if (el.scrollHeight > el.clientHeight + 250 && el.clientHeight > 250) {
                        el.scrollTop += 1700;
                    }
                }
                const btns = Array.from(document.querySelectorAll('button, div, span'));
                for (const b of btns) {
                    const t = (b.textContent || '').trim();
                    if (t === '顯示更多' || t === '更多賽事' || t === 'Load More') {
                        try { b.click(); } catch (e) {}
                    }
                }
            }"""
        )
        await page.wait_for_timeout(240)


async def extract_dom_fallback(page, collected, now_ms, now_dt):
    """Backup DOM extractor on /ch/football/hil so visible FB rows & live odds are never missed."""
    try:
        raw_blocks = await page.evaluate(
            """() => {
                const out = [];
                const walker = document.querySelectorAll('div, tr, li, section');
                for (const el of walker) {
                    const txt = (el.innerText || '').trim();
                    if (txt.length >= 18 && txt.length <= 360 && /FB\\d{4}/.test(txt) && /\\d+\\.\\d{2}/.test(txt)) {
                        out.push(txt);
                    }
                }
                return out;
            }"""
        )
    except Exception:
        return 0

    added_or_updated = 0
    for blk in raw_blocks:
        m_fb = re.search(r"(FB\d{4})", blk)
        if not m_fb:
            continue
        fid = m_fb.group(1).upper()

        m_ko = re.search(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", blk)
        ko_ms = 0
        if m_ko:
            day_i, mon_i, hr_i, min_i = map(int, m_ko.groups())
            yr = now_dt.year
            if mon_i == 1 and now_dt.month == 12:
                yr += 1
            try:
                ko_dt = datetime(yr, mon_i, day_i, hr_i, min_i, tzinfo=HKT)
                ko_ms = int(ko_dt.timestamp() * 1000)
            except Exception:
                ko_ms = 0

        if ko_ms > 0 and ko_ms < (now_ms - 120_000):
            continue

        m_teams = re.search(r"([^\n\r\d]{2,18})\s*(?:對|vs|VS|v)\s*([^\n\r\d]{2,18})", blk)
        home_ch = m_teams.group(1).strip() if m_teams else ""
        away_ch = m_teams.group(2).strip() if m_teams else ""

        m_line = re.search(r"(?:球線|\b)(1\.5/2\.0|2\.0/2\.5|2\.5/3\.0|3\.0/3\.5|2\.5|2\.0|3\.0|3\.5)(?:\b)", blk)
        line_str = m_line.group(1) if m_line else "2.5"

        odds_found = [float(x) for x in re.findall(r"\b(\d+\.\d{2})\b", blk) if 1.08 <= float(x) <= 8.50]
        if len(odds_found) >= 2 and (home_ch or fid in collected):
            h_odd, l_odd = odds_found[0], odds_found[1]
            ex = collected.get(fid)
            if ex:
                # Only fill missing fields; GraphQL remains authoritative if already present
                if ko_ms > 0 and not ex.get("kickoffMs"):
                    ex["kickoffMs"] = ko_ms
            elif home_ch and away_ch:
                upsert_match_record(
                    collected, fid, "足球賽事", home_ch, away_ch, "", "",
                    ko_ms, (line_str, h_odd, l_odd), {"H": 0.0, "D": 0.0, "A": 0.0}
                )
                added_or_updated += 1
    return added_or_updated


async def main():
    prev_mem = load_previous_memory()
    now_dt = datetime.now(HKT)
    now_ms = int(now_dt.timestamp() * 1000)
    now_label = now_dt.strftime("%d/%m %H:%M")

    collected = {}
    gql_templates = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        # CRITICAL: Do NOT set custom extra_http_headers here, or HKJC GraphQL CORS preflight blocks!
        context = await browser.new_context(
            locale="zh-HK",
            timezone_id="Asia/Hong_Kong",
            viewport={"width": 1440, "height": 2400},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Disable Chromium HTTP cache natively via CDP without injecting any CORS-breaking headers
        try:
            cdp = await context.new_cdp_session(page)
            await cdp.send("Network.enable")
            await cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
        except Exception as e:
            print(f"CDP cache-disable note: {e}")

        current_stage = "INIT"

        async def handle_response(resp):
            url = resp.url
            if "graphql" not in url.lower() and "info.cld.hkjc.com" not in url.lower():
                return
            try:
                req = resp.request
                if req.method == "POST" and req.post_data:
                    parsed_post = json.loads(req.post_data)
                    hdrs = {
                        k: v for k, v in req.headers.items()
                        if k.lower() not in ("content-length", "host", "connection")
                    }
                    gql_templates[current_stage] = (url, parsed_post, hdrs)
                payload = await resp.json()
                walk_graphql_matches(payload, collected, now_ms)
            except Exception:
                pass

        page.on("response", handle_response)

        # Pass 1: Full HAD page (all matches cross-pool 1X2 odds)
        current_stage = "HAD"
        try:
            await page.goto("https://bet.hkjc.com/ch/football/had", wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_timeout(2800)
            await scroll_all_containers(page, steps=16)
        except Exception as e:
            print(f"HAD navigation note: {e}")

        # Pass 2: Full HIL page (authoritative latest HiLo lines & live odds)
        current_stage = "HIL"
        try:
            await page.goto("https://bet.hkjc.com/ch/football/hil", wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_timeout(3200)
            await scroll_all_containers(page, steps=24)
            await extract_dom_fallback(page, collected, now_ms, now_dt)
        except Exception as e:
            print(f"HIL navigation note: {e}")

        # Pass 3: CORS-free native APIRequestContext pagination (1..400) for both HAD and HIL
        for stage_key in ("HAD", "HIL"):
            tmpl = gql_templates.get(stage_key)
            if not tmpl:
                continue
            gql_url, base_body, req_hdrs = tmpl
            for start_idx in (1, 101, 201, 301):
                patched_body = copy.deepcopy(base_body)
                if not patch_pagination_vars(patched_body, start_idx, start_idx + 99):
                    break
                try:
                    api_resp = await context.request.post(
                        gql_url,
                        data=json.dumps(patched_body),
                        headers=req_hdrs,
                        timeout=15000
                    )
                    if api_resp.ok:
                        res_json = await api_resp.json()
                        walk_graphql_matches(res_json, collected, now_ms)
                except Exception as e:
                    print(f"Pagination note ({stage_key} {start_idx}): {e}")

        await context.close()
        await browser.close()

    scraped_fresh_count = len(collected)
    print(f"Freshly scraped from HKJC on this run: {scraped_fresh_count} matches.")

    if scraped_fresh_count == 0:
        print("ERROR: 0 live matches extracted from HKJC! Failing loudly so stale data is never masked.", file=sys.stderr)
        sys.exit(1)

    final_matches = []
    changed_count = 0
    new_matches_count = 0

    for mid, cur in collected.items():
        if cur["kickoffMs"] > 0 and cur["kickoffMs"] < (now_ms - 120_000):
            continue

        old = prev_mem.get(mid)
        is_brand_new = old is None
        if is_brand_new and len(prev_mem) > 0:
            new_matches_count += 1

        old = old or {}
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
            history = history[max(0, len(history) - 14):]

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
            "isNewMatch": bool(is_brand_new and len(prev_mem) > 0),
            "oddsHistory": history,
            "updatedAtHkt": now_label
        })
        final_matches.append(cur)

    final_matches.sort(key=lambda x: (x["kickoffMs"] if x["kickoffMs"] > 0 else 9_999_999_999_999, x["id"]))

    os.makedirs("data", exist_ok=True)
    output_payload = {
        "version": "v20",
        "updatedAtIso": now_dt.isoformat(),
        "updatedAtHkt": now_dt.strftime("%d/%m (%a) %H:%M:%S HKT"),
        "matchCount": len(final_matches),
        "scrapedFreshCount": scraped_fresh_count,
        "newMatchesAddedCount": new_matches_count,
        "oddsChangedCount": changed_count,
        "matches": final_matches
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, ensure_ascii=False, indent=2)

    print(
        f"Done ({now_label} HKT): {len(final_matches)} live matches saved | "
        f"+{new_matches_count} new matches | {changed_count} odds updated."
    )


if __name__ == "__main__":
    asyncio.run(main())
