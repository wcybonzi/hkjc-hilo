import asyncio
import json
import os
import re
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright

HKT = timezone(timedelta(hours=8))
HKJC_HILO_URL = "https://bet.hkjc.com/ch/football/hil"
OUTPUT_PATH = "data/hkjc_hilo.json"


def normalize_hkjc_kickoff(raw_val, fallback_date="") -> str:
    """將馬會 GraphQL 或網頁各種日期與開賽時間格式統一轉為 YYYY-MM-DDTHH:MM:00+08:00"""
    if raw_val is None:
        return ""

    # 1. 處理毫秒或秒級數字時間戳 (Epoch ms / s)
    if isinstance(raw_val, (int, float)) or re.fullmatch(r"\d{10,13}", str(raw_val).strip()):
        num = float(raw_val)
        if num > 1e11:
            num /= 1000.0
        dt = datetime.fromtimestamp(num, tz=HKT)
        return dt.strftime("%Y-%m-%dT%H:%M:00+08:00")

    s = str(raw_val).strip()
    if not s:
        return ""

    # 2. 標準完整 ISO 時間 (必須同時包含日期與 HH:MM，避免誤收僅得日期的 matchDate 如 2026-11-24+08:00)
    m_iso = re.search(r"(\d{4})-(\d{2})-(\d{2})[T\s]+(\d{2}):(\d{2})", s)
    if m_iso:
        yyyy, mm, dd, hh, mn = m_iso.groups()
        # 若為 UTC Z 時間則換算至 +08:00
        if s.endswith("Z"):
            dt_utc = datetime(int(yyyy), int(mm), int(dd), int(hh), int(mn), tzinfo=timezone.utc)
            return dt_utc.astimezone(HKT).strftime("%Y-%m-%dT%H:%M:00+08:00")
        return f"{yyyy}-{mm}-{dd}T{hh}:{mn}:00+08:00"

    # 3. DD/MM/YYYY HH:MM
    m_dmy = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})[^\d]+(\d{1,2}):(\d{2})", s)
    if m_dmy:
        dd, mm, yyyy, hh, mn = m_dmy.groups()
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}T{int(hh):02d}:{mn}:00+08:00"

    # 4. DD/MM HH:MM (支援中間夾雜星期或換行，並自動處理跨年)
    m_dm = re.search(r"(\d{1,2})/(\d{1,2})(?!/)[^\d]{1,12}(\d{1,2}):(\d{2})", s)
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

    # 5. 若 kickOffTime 僅提供 HH:MM，則結合 matchDate 日期欄位
    m_hm = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", s)
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
    cleaned = re.sub(r"\d{1,2}/\d{1,2}(?:/\d{4})?\s*(?:\([周星期一二三四五六日]\))?\s*\d{1,2}:\d{2}", "", str(name))
    cleaned = re.sub(r"預售|即場", "", cleaned)
    return cleaned.strip()


def extract_from_graphql_payload(payload: dict) -> list:
    extracted = []

    def walk(node):
        if isinstance(node, dict):
            code = node.get("frontEndId") or node.get("id") or ""
            if isinstance(code, str) and code.startswith("FB") and ("foPools" in node or "homeTeam" in node):
                home_obj = node.get("homeTeam") or {}
                away_obj = node.get("awayTeam") or {}
                tourn_obj = node.get("tournament") or {}

                home = clean_team_name(home_obj.get("name_ch") or home_obj.get("nameCh") or "")
                away = clean_team_name(away_obj.get("name_ch") or away_obj.get("nameCh") or "")
                en_home = (home_obj.get("name_en") or home_obj.get("nameEn") or "").strip()
                en_away = (away_obj.get("name_en") or away_obj.get("nameEn") or "").strip()
                league = (tourn_obj.get("name_ch") or tourn_obj.get("nameCh") or "足智彩賽事").strip()

                # 優先讀取含時鐘時間之 kickOffTime / kickoffTime / startTime / esst，切勿先讀僅得日期的 matchDate
                raw_ko = (
                    node.get("kickOffTime")
                    or node.get("kickoffTime")
                    or node.get("startTime")
                    or node.get("esst")
                    or node.get("matchTime")
                    or ""
                )
                raw_date_only = node.get("matchDate") or node.get("businessDate") or ""
                kickoff_iso = normalize_hkjc_kickoff(raw_ko, fallback_date=raw_date_only)
                if not kickoff_iso and raw_date_only:
                    kickoff_iso = normalize_hkjc_kickoff(raw_date_only)

                for pool in node.get("foPools", []):
                    if pool.get("oddsType") == "HIL":
                        for line in pool.get("lines", []):
                            cond = str(line.get("condition") or "").strip()
                            if cond in ("2.5", "2.5/2.5"):
                                combs = line.get("combinations", [])
                                high_odds, low_odds = None, None
                                for c in combs:
                                    sel = str(c.get("str") or c.get("selectionId") or "").upper()
                                    val = c.get("currentOdds")
                                    if sel in ("H", "HIGH", "大") and val:
                                        high_odds = float(val)
                                    elif sel in ("L", "LOW", "細") and val:
                                        low_odds = float(val)
                                if high_odds and low_odds:
                                    extracted.append({
                                        "code": code,
                                        "league": league,
                                        "home": home,
                                        "away": away,
                                        "enHome": en_home,
                                        "enAway": en_away,
                                        "kickoffIso": kickoff_iso,
                                        "oddsHigh": high_odds,
                                        "oddsLow": low_odds,
                                    })
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return extracted


async def scrape_hkjc_hilo():
    captured_matches = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="zh-HK",
            timezone_id="Asia/Hong_Kong",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        async def handle_response(response):
            if "graphql" in response.url.lower() and response.status == 200:
                try:
                    body = await response.json()
                    for m in extract_from_graphql_payload(body):
                        existing = captured_matches.get(m["code"], {})
                        if not m["kickoffIso"] and existing.get("kickoffIso"):
                            m["kickoffIso"] = existing["kickoffIso"]
                        captured_matches[m["code"]] = m
                except Exception:
                    pass

        page.on("response", handle_response)
        await page.goto(HKJC_HILO_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(6000)

        # 網頁文字雙重掃描：同時支援開賽時間位於隊名之前或之後，並補齊任何遺漏之 kickoffIso
        body_text = await page.inner_text("body")
        chunks = re.split(r"(?=FB\d{4})", body_text)
        for chunk in chunks:
            m_code = re.match(r"(FB\d{4})", chunk.strip())
            if not m_code:
                continue
            code = m_code.group(1)
            window = chunk[:420]

            m_odds = re.search(r"\[2\.5\]\s*(\d+\.\d{2})\s+(\d+\.\d{2})", window)
            m_time = re.search(r"(\d{1,2}/\d{1,2}(?:/\d{4})?[\s\n]*(?:\([周星期一二三四五六日]\))?[\s\n]*\d{1,2}:\d{2})", window)
            ko_iso = normalize_hkjc_kickoff(m_time.group(1)) if m_time else ""

            if code in captured_matches:
                if ko_iso and not captured_matches[code].get("kickoffIso"):
                    captured_matches[code]["kickoffIso"] = ko_iso
                continue

            if m_odds:
                m_teams = re.search(r"([^\n\[]+?)\s+對\s+([^\n\[]+)", window)
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
    output_doc = {
        "updatedAtHkt": datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S HKT"),
        "source": HKJC_HILO_URL,
        "count": len(sorted_matches),
        "matches": sorted_matches,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output_doc, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(scrape_hkjc_hilo())
