import asyncio
import json
import os
import re
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright

HKT = timezone(timedelta(hours=8))
HKJC_HILO_URL = "https://bet.hkjc.com/ch/football/hil"
OUTPUT_PATH = "data/hkjc_hilo.json"


def normalize_hkjc_kickoff(raw_val, fallback_date: str = "") -> str:
    """Convert any HKJC GraphQL or webpage date/time into YYYY-MM-DDTHH:MM:00+08:00."""
    if raw_val is None:
        return ""

    # 1. Epoch seconds or milliseconds
    raw_str = str(raw_val).strip()
    if not raw_str:
        return ""
    if isinstance(raw_val, (int, float)) or re.fullmatch(r"\d{10,13}", raw_str):
        num = float(raw_str)
        if num > 1e11:
            num /= 1000.0
        dt = datetime.fromtimestamp(num, tz=HKT)
        return dt.strftime("%Y-%m-%dT%H:%M:00+08:00")

    # 2. Full ISO timestamp with clock time (ignores date-only strings like 2026-11-24+08:00)
    m_iso = re.search(r"(\d{4})-(\d{2})-(\d{2})[T\s]+(\d{2}):(\d{2})", raw_str)
    if m_iso:
        yyyy, mm, dd, hh, mn = m_iso.groups()
        if raw_str.endswith("Z"):
            dt_utc = datetime(int(yyyy), int(mm), int(dd), int(hh), int(mn), tzinfo=timezone.utc)
            return dt_utc.astimezone(HKT).strftime("%Y-%m-%dT%H:%M:00+08:00")
        return f"{yyyy}-{mm}-{dd}T{hh}:{mn}:00+08:00"

    # 3. DD/MM/YYYY HH:MM
    m_dmy = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})[^\d]+(\d{1,2}):(\d{2})", raw_str)
    if m_dmy:
        dd, mm, yyyy, hh, mn = m_dmy.groups()
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}T{int(hh):02d}:{mn}:00+08:00"

    # 4. DD/MM HH:MM (handles weekdays or line breaks in between and year rollover)
    m_dm = re.search(r"(\d{1,2})/(\d{1,2})(?!/)[^\d]{1,14}(\d{1,2}):(\d{2})", raw_str)
    if m_dm:
        dd, mm, hh, mn = m_dm.groups()
        now_hkt = datetime.now(HKT)
        yyyy = now_hkt.year
        mm_int = int(mm)
        if now_hkt.month == 12 and mm_int == 1:
            yyyy += 1
        elif now_hkt.month == 1 and mm_int == 12:
            yyyy -= 1
        return f"{yyyy}-{mm_int:02d}-{int(dd):02d}T{int(hh):02d}:{mn}:00+08:00"

    # 5. Clock-only HH:MM paired with matchDate
    m_hm = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", raw_str)
    if m_hm and fallback_date:
        hh, mn = m_hm.groups()
        m_fd = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(fallback_date))
        if m_fd:
            yyyy, mm, dd = m_fd.groups()
            return f"{yyyy}-{mm}-{dd}T{int(hh):02d}:{mn}:00+08:00"
        m_fd2 = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(fallback_date))
        if m_fd2:
            dd, mm, yyyy = m_fd2.groups()
            return f"{yyyy}-{int(mm):02d}-{int(dd):02d}T{int(hh):02d}:{mn}:00+08:00"

    return ""


def clean_team_name(name: str) -> str:
    if not name:
        return ""
    cleaned = re.sub(
        r"\d{1,2}/\d{1,2}(?:/\d{4})?\s*(?:\([周星期一二三四五六日]\))?\s*\d{1,2}:\d{2}",
        "",
        str(name),
    )
    cleaned = re.sub(r"預售|即場|主其他|客其他", "", cleaned)
    return cleaned.strip()


def find_fb_code(node: dict) -> str:
    """Extract FBxxxx code from any HKJC ID field."""
    for key in ("frontEndId", "fbId", "matchCode", "code", "id"):
        val = node.get(key)
        if isinstance(val, str):
            m = re.search(r"FB\d{4}", val.upper())
            if m:
                return m.group(0)
    return ""


def extract_hil_25_from_pools(pools: list) -> tuple:
    """Return (high_odds, low_odds) for HiLo 2.5 from foPools list."""
    if not isinstance(pools, list):
        return (None, None)

    for pool in pools:
        if not isinstance(pool, dict):
            continue
        odds_type = str(pool.get("oddsType") or pool.get("poolType") or "").upper()
        if odds_type and odds_type != "HIL":
            continue

        lines = pool.get("lines") or []
        if not isinstance(lines, list):
            continue

        for line in lines:
            if not isinstance(line, dict):
                continue
            cond = str(line.get("condition") or line.get("line") or "").strip()
            is_25 = cond in ("2.5", "2.50", "2.5/2.5", "[2.5]")
            if not is_25:
                try:
                    is_25 = abs(float(cond) - 2.5) < 1e-6
                except ValueError:
                    is_25 = False
            if not is_25:
                continue

            combs = line.get("combinations") or line.get("selections") or []
            high_odds, low_odds = None, None
            for c in combs:
                if not isinstance(c, dict):
                    continue
                sel = str(
                    c.get("str")
                    or c.get("selectionId")
                    or c.get("combId")
                    or c.get("name_ch")
                    or c.get("nameCh")
                    or ""
                ).strip().upper()
                raw_odds = c.get("currentOdds") or c.get("odds")
                try:
                    val = float(raw_odds) if raw_odds is not None else None
                except (TypeError, ValueError):
                    val = None
                if not val or val <= 1.0:
                    continue

                if sel in ("H", "HIGH", "OVER", "大", "01", "1"):
                    high_odds = val
                elif sel in ("L", "LOW", "UNDER", "細", "02", "2"):
                    low_odds = val

            # Fallback if HKJC returns exactly 2 selections [High, Low] without labels
            if (high_odds is None or low_odds is None) and len(combs) == 2:
                try:
                    o1 = float(combs[0].get("currentOdds") or combs[0].get("odds"))
                    o2 = float(combs[1].get("currentOdds") or combs[1].get("odds"))
                    if o1 > 1.0 and o2 > 1.0:
                        high_odds, low_odds = o1, o2
                except Exception:
                    pass

            if high_odds and low_odds:
                return (high_odds, low_odds)

    return (None, None)


def collect_graphql_data(payload, meta_store: dict, odds_store: dict) -> None:
    """Index match metadata and HIL 2.5 odds separately so split GraphQL queries merge cleanly."""

    def walk(node):
        if isinstance(node, dict):
            raw_id = str(node.get("id") or node.get("matchId") or "").strip()
            fb_code = find_fb_code(node)
            keys = [k for k in (fb_code, raw_id) if k]

            # Capture match metadata (teams, tournament, kickOffTime)
            if keys and ("homeTeam" in node or "kickOffTime" in node or "kickoffTime" in node):
                home_obj = node.get("homeTeam") if isinstance(node.get("homeTeam"), dict) else {}
                away_obj = node.get("awayTeam") if isinstance(node.get("awayTeam"), dict) else {}
                tourn_obj = node.get("tournament") if isinstance(node.get("tournament"), dict) else {}

                home = clean_team_name(home_obj.get("name_ch") or home_obj.get("nameCh") or "")
                away = clean_team_name(away_obj.get("name_ch") or away_obj.get("nameCh") or "")
                en_home = str(home_obj.get("name_en") or home_obj.get("nameEn") or "").strip()
                en_away = str(away_obj.get("name_en") or away_obj.get("nameEn") or "").strip()
                league = str(tourn_obj.get("name_ch") or tourn_obj.get("nameCh") or "").strip()

                raw_ko = (
                    node.get("kickOffTime")
                    or node.get("kickoffTime")
                    or node.get("startTime")
                    or node.get("esst")
                    or node.get("matchTime")
                    or ""
                )
                raw_date_only = node.get("matchDate") or node.get("businessDate") or ""
                ko_iso = normalize_hkjc_kickoff(raw_ko, fallback_date=raw_date_only)

                info = {
                    "code": fb_code,
                    "rawId": raw_id,
                    "league": league,
                    "home": home,
                    "away": away,
                    "enHome": en_home,
                    "enAway": en_away,
                    "kickoffIso": ko_iso,
                }
                for k in keys:
                    existing = meta_store.get(k, {})
                    merged = {
                        mk: (mv if mv else existing.get(mk, ""))
                        for mk, mv in info.items()
                    }
                    meta_store[k] = merged

            # Capture HIL [2.5] pool odds
            if "foPools" in node and isinstance(node["foPools"], list):
                h_odds, l_odds = extract_hil_25_from_pools(node["foPools"])
                if h_odds and l_odds and keys:
                    for k in keys:
                        odds_store[k] = (h_odds, l_odds)
            elif str(node.get("oddsType") or "").upper() == "HIL":
                pool_match_id = str(
                    node.get("matchId") or node.get("foMatchId") or node.get("frontEndId") or ""
                ).strip()
                h_odds, l_odds = extract_hil_25_from_pools([node])
                if h_odds and l_odds and pool_match_id:
                    odds_store[pool_match_id] = (h_odds, l_odds)

            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)


async def scrape_hkjc_hilo():
    meta_store = {}
    odds_store = {}
    captured_matches = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            locale="zh-HK",
            timezone_id="Asia/Hong_Kong",
            viewport={"width": 1440, "height": 2200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        async def handle_response(response):
            url_lower = response.url.lower()
            if response.status == 200 and ("graphql" in url_lower or "info.cld.hkjc.com" in url_lower or "consvc" in url_lower):
                try:
                    body = await response.json()
                    collect_graphql_data(body, meta_store, odds_store)
                except Exception:
                    pass

        page.on("response", handle_response)

        try:
            # Use domcontentloaded instead of networkidle so background HKJC polling never times out
            await page.goto(HKJC_HILO_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception as nav_err:
            print(f"Navigation warning (continuing): {nav_err}")

        await page.wait_for_timeout(6000)

        # Scroll down smoothly to trigger any lazy-loaded HKJC match rows
        for _ in range(5):
            try:
                await page.evaluate("window.scrollBy(0, 900)")
                await page.wait_for_timeout(1200)
            except Exception:
                break

        # 1. Merge GraphQL metadata + HIL [2.5] odds
        for key, meta in meta_store.items():
            fb_code = meta.get("code") or (key if str(key).startswith("FB") else "")
            if not fb_code:
                continue
            odds_pair = odds_store.get(fb_code) or odds_store.get(meta.get("rawId", ""))
            if not odds_pair:
                continue
            captured_matches[fb_code] = {
                "code": fb_code,
                "league": meta.get("league") or "足智彩入球大細",
                "home": meta.get("home") or f"{fb_code} 主隊",
                "away": meta.get("away") or "客隊",
                "enHome": meta.get("enHome") or "",
                "enAway": meta.get("enAway") or "",
                "kickoffIso": meta.get("kickoffIso") or "",
                "oddsHigh": odds_pair[0],
                "oddsLow": odds_pair[1],
            }

        # 2. Webpage text fallback & kick-off time backfill
        try:
            body_text = await page.inner_text("body")
        except Exception:
            body_text = ""

        if body_text:
            chunks = re.split(r"(?=FB\d{4})", body_text)
            for chunk in chunks:
                m_code = re.match(r"(FB\d{4})", chunk.strip())
                if not m_code:
                    continue
                code = m_code.group(1)
                window = chunk[:500]

                m_time = re.search(
                    r"(\d{1,2}/\d{1,2}(?:/\d{4})?[\s\n]*(?:\([周星期一二三四五六日]\))?[\s\n]*\d{1,2}:\d{2})",
                    window,
                )
                ko_iso = normalize_hkjc_kickoff(m_time.group(1)) if m_time else ""

                if code in captured_matches:
                    if ko_iso and not captured_matches[code].get("kickoffIso"):
                        captured_matches[code]["kickoffIso"] = ko_iso
                    continue

                # Supports both "[2.5] 1.85 1.85" and "2.5 1.85 1.85" across spaces or newlines
                m_odds = re.search(
                    r"\[?2\.5\]?[\s\n]+(\d+\.\d{2})[\s\n]+(\d+\.\d{2})",
                    window,
                )
                if m_odds:
                    m_teams = re.search(r"([^\n\[]+?)\s*(?:對|vs|VS)\s*([^\n\[]+)", window)
                    home = clean_team_name(m_teams.group(1)) if m_teams else f"{code} 主隊"
                    away = clean_team_name(m_teams.group(2)) if m_teams else "客隊"
                    captured_matches[code] = {
                        "code": code,
                        "league": "足智彩入球大細",
                        "home": home,
                        "away": away,
                        "enHome": "",
                        "enAway": "",
                        "kickoffIso": ko_iso,
                        "oddsHigh": float(m_odds.group(1)),
                        "oddsLow": float(m_odds.group(2)),
                    }

        await browser.close()

    sorted_matches = sorted(
        captured_matches.values(),
        key=lambda x: (x.get("kickoffIso") == "", x.get("kickoffIso") or "9999", x.get("code") or ""),
    )

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    # Preserve existing matches if HKJC is temporarily under maintenance (0 matches returned)
    if not sorted_matches and os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, "r", encoding="utf-8") as prev_f:
                prev_doc = json.load(prev_f)
                sorted_matches = prev_doc.get("matches", [])
        except Exception:
            pass

    output_doc = {
        "updatedAtHkt": datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S HKT"),
        "source": HKJC_HILO_URL,
        "count": len(sorted_matches),
        "matches": sorted_matches,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output_doc, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(sorted_matches)} HKJC HiLo [2.5] matches to {OUTPUT_PATH}")


if __name__ == "__main__":
    asyncio.run(scrape_hkjc_hilo())
