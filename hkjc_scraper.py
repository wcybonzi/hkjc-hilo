import asyncio
import json
import os
import re
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright

HKT = timezone(timedelta(hours=8))
HKJC_HILO_URL = "https://bet.hkjc.com/ch/football/hil"
OUTPUT_PATH = "data/hkjc_hilo.json"


def normalize_hkjc_kickoff(raw_str):
    if not raw_str:
        return ""
    raw_str = str(raw_str).strip()
    if "T" in raw_str:
        return raw_str
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})", raw_str)
    if m:
        dd, mm, yyyy, hh, mn = m.groups()
        return f"{yyyy}-{mm}-{dd}T{hh}:{mn}:00+08:00"
    return raw_str


def extract_from_graphql_payload(payload, results_by_code):
    if not isinstance(payload, dict):
        return
    data = payload.get("data") or {}
    matches = data.get("matches") or data.get("matchList") or []
    if not isinstance(matches, list):
        return

    for m in matches:
        code = m.get("frontEndId") or m.get("code") or ""
        if not code.startswith("FB"):
            continue

        home = (m.get("homeTeam") or {}).get("name_ch") or ""
        away = (m.get("awayTeam") or {}).get("name_ch") or ""
        league = (m.get("tournament") or {}).get("name_ch") or "足智彩賽事"
        kickoff = normalize_hkjc_kickoff(m.get("matchDate") or m.get("kickOffTime") or "")

        pools = m.get("foPools") or m.get("pools") or []
        odds_high, odds_low = None, None

        for pool in pools:
            odds_type = (pool.get("oddsType") or "").upper()
            if odds_type not in ("HIL", "HILO"):
                continue
            for line in pool.get("lines") or []:
                cond = str(line.get("condition") or "").strip()
                if cond in ("2.5", "2.5/2.5", "[2.5]"):
                    for comb in line.get("combinations") or []:
                        sel = (comb.get("str") or comb.get("selectionId") or "").upper()
                        price = comb.get("currentOdds") or comb.get("odds")
                        if price:
                            if sel in ("H", "HIGH", "大"):
                                odds_high = float(price)
                            elif sel in ("L", "LOW", "細"):
                                odds_low = float(price)

        if odds_high and odds_low:
            results_by_code[code] = {
                "code": code,
                "league": league,
                "home": home,
                "away": away,
                "kickoffIso": kickoff,
                "oddsHigh": round(odds_high, 2),
                "oddsLow": round(odds_low, 2),
                "source": "HKJC_GraphQL"
            }


async def scrape_hkjc_hilo():
    results_by_code = {}
    now_year = datetime.now(HKT).year

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="zh-HK",
            timezone_id="Asia/Hong_Kong",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        async def handle_response(response):
            if "graphql" in response.url and response.status == 200:
                try:
                    body = await response.json()
                    if isinstance(body, list):
                        for item in body:
                            extract_from_graphql_payload(item, results_by_code)
                    else:
                        extract_from_graphql_payload(body, results_by_code)
                except Exception:
                    pass

        page.on("response", handle_response)

        await page.goto(HKJC_HILO_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)

        body_text = await page.inner_text("body")
        pattern = re.compile(
            r"(FB\d{4})\s+([^\n]+?)\s+對\s+([^\n]+?)[\s\S]{0,180}?(?:(\d{2})/(\d{2})\s+(\d{2}):(\d{2})[\s\S]{0,80}?)?\[2\.5\]\s*(\d+\.\d{2})\s+(\d+\.\d{2})"
        )
        for match in pattern.finditer(body_text):
            code, home, away, dd, mm, hh, mn, hi, lo = match.groups()
            ko_iso = f"{now_year}-{mm}-{dd}T{hh}:{mn}:00+08:00" if dd and mm and hh and mn else ""
            if code not in results_by_code:
                results_by_code[code] = {
                    "code": code.strip(),
                    "league": "足智彩賽事",
                    "home": home.strip(),
                    "away": away.strip(),
                    "kickoffIso": ko_iso,
                    "oddsHigh": float(hi),
                    "oddsLow": float(lo),
                    "source": "HKJC_DOM"
                }

        await browser.close()

    sorted_matches = sorted(
        results_by_code.values(),
        key=lambda x: x.get("kickoffIso") or "9999-12-31T23:59:00+08:00"
    )

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    output_data = {
        "updatedAt": datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S HKT"),
        "count": len(sorted_matches),
        "matches": sorted_matches
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(scrape_hkjc_hilo())
