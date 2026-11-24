import asyncio
import json
import math
import os
import re
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from itertools import islice
from playwright.async_api import async_playwright

HKT = timezone(timedelta(hours=8))
HKJC_HILO_CH_URL = "https://bet.hkjc.com/ch/football/hil"
HKJC_HILO_EN_URL = "https://bet.hkjc.com/en/football/hil"
OUTPUT_PATH = "data/hkjc_hilo.json"

WEEKDAY_MAP = {
    "周一": 0, "星期一": 0, "MON": 0,
    "周二": 1, "星期二": 1, "TUE": 1,
    "周三": 2, "星期三": 2, "WED": 2,
    "周四": 3, "星期四": 3, "THU": 3,
    "周五": 4, "星期五": 4, "FRI": 4,
    "周六": 5, "星期六": 5, "SAT": 5,
    "周日": 6, "星期日": 6, "SUN": 6,
}


def at(seq, idx, default=None):
    if not seq or idx < 0:
        return default
    return next(islice(seq, idx, None), default)


def resolve_year_for_month(mm_int: int) -> int:
    now_hkt = datetime.now(HKT)
    yyyy = now_hkt.year
    if now_hkt.month == 12 and mm_int == 1:
        yyyy += 1
    elif now_hkt.month == 1 and mm_int == 12:
        yyyy -= 1
    return yyyy


def resolve_date_from_weekday(wk_token: str, hh_int: int) -> str:
    now_hkt = datetime.now(HKT)
    target_wk = WEEKDAY_MAP.get(wk_token.upper())
    if target_wk is None:
        return ""
    days_ahead = (target_wk - now_hkt.weekday()) % 7
    # 馬會「周六」賽事若於凌晨 00:00–05:59 開賽，實際日曆通常為翌晨（周日凌晨）
    if 0 <= hh_int <= 5:
        days_ahead = (days_ahead + 1) % 7
    cand = now_hkt + timedelta(days=days_ahead)
    return cand.strftime("%Y-%m-%d")


def normalize_hkjc_kickoff(raw_val, fallback_date="") -> str:
    """支援馬會 GraphQL 及網頁所有日期時間格式（含跨行、中間夾雜 FB 編號/周次、分欄日期+時間）"""
    if raw_val is None:
        return ""
    if isinstance(raw_val, (int, float)) or re.fullmatch(r"\d{10,13}", str(raw_val).strip()):
        num = float(raw_val)
        if num > 1e11:
            num /= 1000.0
        return datetime.fromtimestamp(num, tz=HKT).strftime("%Y-%m-%dT%H:%M:00+08:00")

    s = str(raw_val).strip()
    if not s:
        return ""

    # 修正 +0800 -> +08:00
    s_norm = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)

    # 1. ISO 日期時間 YYYY-MM-DDTHH:MM
    if re.search(r"\d{4}-\d{2}-\d{2}[T\s]+\d{1,2}:\d{2}", s_norm):
        try:
            iso_clean = s_norm.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_clean)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=HKT)
            return dt.astimezone(HKT).strftime("%Y-%m-%dT%H:%M:00+08:00")
        except ValueError:
            m_iso = re.search(r"(\d{4})-(\d{2})-(\d{2})[T\s]+(\d{1,2}):(\d{2})", s_norm)
            if m_iso:
                yyyy, mm, dd, hh, mn = m_iso.groups()
                return f"{yyyy}-{mm}-{dd}T{int(hh):02d}:{mn}:00+08:00"

    # 先獨立尋找字串內之時鐘時間 HH:MM（排除賠率小數點，僅匹配冒號時間）
    m_clock = re.search(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d])", s)
    if not m_clock:
        return ""
    hh_int, mn_str = int(m_clock.group(1)), m_clock.group(2)

    # 2. 字串本身或 fallback_date 含 DD/MM/YYYY
    combo = f"{s} {fallback_date}"
    m_dmy = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", combo)
    if m_dmy:
        dd, mm, yyyy = m_dmy.groups()
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}T{hh_int:02d}:{mn_str}:00+08:00"

    # 3. 字串或 fallback_date 含 YYYY-MM-DD
    m_ymd = re.search(r"(\d{4})-(\d{2})-(\d{2})", combo)
    if m_ymd:
        yyyy, mm, dd = m_ymd.groups()
        return f"{yyyy}-{mm}-{dd}T{hh_int:02d}:{mn_str}:00+08:00"

    # 4. 中文日期 MM月DD日
    m_zh = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日", combo)
    if m_zh:
        yyyy_raw, mm, dd = m_zh.groups()
        mm_int = int(mm)
        yyyy = int(yyyy_raw) if yyyy_raw else resolve_year_for_month(mm_int)
        return f"{yyyy}-{mm_int:02d}-{int(dd):02d}T{hh_int:02d}:{mn_str}:00+08:00"

    # 5. DD/MM（排除亞洲讓球盤線如 2/2.5、2.5/3）
    for m_dm in re.finditer(r"(?<![\.\d])(\d{1,2})/(\d{1,2})(?![/\.\d])", combo):
        dd_int, mm_int = int(m_dm.group(1)), int(m_dm.group(2))
        if 1 <= dd_int <= 31 and 1 <= mm_int <= 12:
            yyyy = resolve_year_for_month(mm_int)
            return f"{yyyy}-{mm_int:02d}-{dd_int:02d}T{hh_int:02d}:{mn_str}:00+08:00"

    # 6. 周次代碼推算（如 周六 15 + 04:00）
    m_wk = re.search(r"(星期[一二三四五六日]|周[一二三四五六日]|\b(?:MON|TUE|WED|THU|FRI|SAT|SUN)\b)", combo, re.IGNORECASE)
    if m_wk:
        ymd = resolve_date_from_weekday(m_wk.group(1), hh_int)
        if ymd:
            return f"{ymd}T{hh_int:02d}:{mn_str}:00+08:00"

    return ""


def scan_nested_kickoff(obj, fallback_date="") -> str:
    if isinstance(obj, dict):
        local_date = str(
            obj.get("matchDate") or obj.get("date") or obj.get("matchDay") or obj.get("frontEndId") or fallback_date or ""
        )
        priority_keys = (
            "kickOffTime", "kickoffTime", "startTime", "matchTime",
            "expectedStartTime", "esst", "bst", "koTime", "time", "kickOff",
        )
        for pk in priority_keys:
            if pk in obj and obj.get(pk) is not None:
                ko = normalize_hkjc_kickoff(obj.get(pk), local_date)
                if ko:
                    return ko
        for k, v in obj.items():
            if k in ("foPools", "lines", "combinations", "odds"):
                continue
            ko = scan_nested_kickoff(v, local_date)
            if ko:
                return ko
    elif isinstance(obj, list):
        for item in obj:
            ko = scan_nested_kickoff(item, fallback_date)
            if ko:
                return ko
    elif isinstance(obj, (str, int, float)):
        s = str(obj).strip()
        if len(s) >= 4:
            return normalize_hkjc_kickoff(obj, fallback_date)
    return ""


def extract_fb_code(node: dict) -> str:
    for key in ("code", "fbId", "matchCode", "frontEndId", "id"):
        val = str(node.get(key) or "").strip()
        m = re.search(r"(FB\d{4})", val)
        if m:
            return m.group(1)
    # 深度搜尋字串欄位內是否帶有 FBxxxx
    for k, val in node.items():
        if isinstance(val, str) and "FB" in val:
            m = re.search(r"(FB\d{4})", val)
            if m:
                return m.group(1)
    return ""


def clean_team_name(name: str) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"\d{1,2}/\d{1,2}(?:/\d{4})?", "", name)
    cleaned = re.sub(r"\d{1,2}:\d{2}", "", cleaned)
    return cleaned.strip()


def norm_en_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9\s]", " ", str(name or "").lower())
    stopwords = {"fc", "sc", "cf", "ac", "afc", "club", "de", "al", "united", "utd", "city", "sporting"}
    tokens = [t for t in s.split() if t not in stopwords and len(t) > 1]
    return " ".join(tokens) if tokens else s.strip()


def team_similarity(a: str, b: str) -> float:
    na, nb = norm_en_name(a), norm_en_name(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 0.92
    return SequenceMatcher(None, na, nb).ratio()


logged_sample_keys = False


def collect_hkjc_graphql(payload, match_meta: dict, kickoff_by_key: dict, hilo_map: dict, had_map: dict):
    global logged_sample_keys
    if isinstance(payload, dict):
        fb_code = extract_fb_code(payload)
        raw_id = str(payload.get("id") or payload.get("matchId") or fb_code or "").strip()

        # 不論是否有 homeTeam，只要節點帶有 ID/FB 編號與時間，即先記錄至 kickoff_by_key
        if raw_id or fb_code:
            ko_any = scan_nested_kickoff(payload, str(payload.get("matchDate") or payload.get("frontEndId") or ""))
            if ko_any:
                for k in (raw_id, fb_code):
                    if k:
                        kickoff_by_key[k] = ko_any

        if isinstance(payload.get("homeTeam"), dict) and isinstance(payload.get("awayTeam"), dict):
            if not logged_sample_keys:
                print(f"Sample HKJC GraphQL match keys: {list(payload.keys())}")
                logged_sample_keys = True
            h_obj, a_obj = payload.get("homeTeam") or {}, payload.get("awayTeam") or {}
            t_obj = payload.get("tournament") or {}
            ko_iso = (
                scan_nested_kickoff(payload, str(payload.get("matchDate") or payload.get("frontEndId") or ""))
                or kickoff_by_key.get(raw_id)
                or kickoff_by_key.get(fb_code)
                or ""
            )
            prev = match_meta.get(fb_code or raw_id, {})
            meta = {
                "id": raw_id,
                "code": fb_code or prev.get("code") or "",
                "league": t_obj.get("name_ch") or prev.get("league") or t_obj.get("name_en") or "足智彩賽事",
                "home": clean_team_name(h_obj.get("name_ch") or prev.get("home") or h_obj.get("name_en") or ""),
                "away": clean_team_name(a_obj.get("name_ch") or prev.get("away") or a_obj.get("name_en") or ""),
                "enHome": h_obj.get("name_en") or prev.get("enHome") or "",
                "enAway": a_obj.get("name_en") or prev.get("enAway") or "",
                "kickoffIso": ko_iso or prev.get("kickoffIso") or "",
            }
            for k in (raw_id, fb_code):
                if k:
                    match_meta[k] = meta

        odds_type = str(payload.get("oddsType") or "")
        owner_keys = [str(payload.get("matchId") or ""), fb_code, raw_id]

        if odds_type == "HIL":
            for line in payload.get("lines") or []:
                cond = str(line.get("condition") or "").strip()
                if cond in ("2.5", "2.50", "2.5/2.5"):
                    h_val, l_val = None, None
                    combs = line.get("combinations") or []
                    for c in combs:
                        tag = str(c.get("str") or c.get("selection") or "").upper()
                        val = c.get("currentOdds") or c.get("odds")
                        if val:
                            if tag in ("H", "HIGH", "OVER", "大"):
                                h_val = float(val)
                            elif tag in ("L", "LOW", "UNDER", "細"):
                                l_val = float(val)
                    if (h_val is None or l_val is None) and len(combs) == 2:
                        c_first, c_second = at(combs, 0, {}), at(combs, 1, {})
                        v1 = c_first.get("currentOdds") or c_first.get("odds")
                        v2 = c_second.get("currentOdds") or c_second.get("odds")
                        if v1 and v2:
                            h_val, l_val = float(v1), float(v2)
                    if h_val and l_val:
                        for ok in owner_keys:
                            if ok:
                                hilo_map[ok] = (h_val, l_val)

        elif odds_type == "HAD":
            for line in payload.get("lines") or []:
                hm, dr, aw = None, None, None
                for c in line.get("combinations") or []:
                    tag = str(c.get("str") or "").upper()
                    val = c.get("currentOdds") or c.get("odds")
                    if val:
                        if tag in ("H", "HOME", "主"):
                            hm = float(val)
                        elif tag in ("D", "DRAW", "和"):
                            dr = float(val)
                        elif tag in ("A", "AWAY", "客"):
                            aw = float(val)
                if hm and dr and aw:
                    for ok in owner_keys:
                        if ok:
                            had_map[ok] = (hm, dr, aw)

        for v in payload.values():
            collect_hkjc_graphql(v, match_meta, kickoff_by_key, hilo_map, had_map)
    elif isinstance(payload, list):
        for item in payload:
            collect_hkjc_graphql(item, match_meta, kickoff_by_key, hilo_map, had_map)


async def harvest_dom_rows_at_scroll(page, dom_row_Store: dict):
    """在每次捲動畫面時即時擷取每個 FBxxxx 獨立列容器文字與最近上方日期標題（克服虛擬捲動卸載問題）"""
    try:
        rows = await page.evaluate(
            """() => {
                const out = [];
                let lastDateBanner = "";
                const dateRe = /(\\d{1,2}\\/\\d{1,2}\\/\\d{4}|\\d{4}年\\d{1,2}月\\d{1,2}日|\\d{1,2}月\\d{1,2}日|(?<![\\.\\d])\\d{1,2}\\/\\d{1,2}(?![\\/\\.\\d]))/;
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
                let node;
                while ((node = walker.nextNode())) {
                    const txt = (node.innerText || "").trim();
                    if (!txt) continue;
                    if (txt.length <= 48 && !txt.includes("FB")) {
                        const dm = txt.match(dateRe);
                        if (dm) lastDateBanner = dm.at(0);
                    }
                    const fbMatches = txt.match(/FB\\d{4}/g);
                    if (fbMatches && fbMatches.length === 1 && txt.length >= 16 && txt.length <= 550) {
                        if (/\\d+\\.\\d{2}/.test(txt) || /\\d{1,2}:\\d{2}/.test(txt)) {
                            out.push({ code: fbMatches.at(0), rowText: txt, bannerDate: lastDateBanner });
                        }
                    }
                }
                return out;
            }"""
        )
        for r in rows or []:
            code = r.get("code")
            if not code:
                continue
            ko = normalize_hkjc_kickoff(r.get("rowText") or "", r.get("bannerDate") or "")
            existing = dom_row_Store.get(code, {})
            dom_row_Store[code] = {
                "rowText": r.get("rowText") if len(r.get("rowText") or "") >= len(existing.get("rowText") or "") else existing.get("rowText"),
                "kickoffIso": ko or existing.get("kickoffIso") or "",
            }
    except Exception:
        pass


async def scroll_and_harvest(page, dom_row_store: dict, steps: int = 6):
    await harvest_dom_rows_at_scroll(page, dom_row_store)
    for _ in range(steps):
        try:
            await page.evaluate(
                """() => {
                    window.scrollBy(0, 1000);
                    document.querySelectorAll('div, main, section').forEach(el => {
                        if (el.scrollHeight > el.clientHeight + 200) el.scrollTop += 1000;
                    });
                }"""
            )
            await page.wait_for_timeout(1000)
            await harvest_dom_rows_at_scroll(page, dom_row_store)
        except Exception:
            break


async def fetch_sofascore_json(page, url: str):
    try:
        return await page.evaluate(
            """async (targetUrl) => {
                const r = await fetch(targetUrl, { headers: { 'Accept': 'application/json' } });
                if (!r.ok) return null;
                return await r.json();
            }""",
            url,
        )
    except Exception:
        return None


def parse_streak_fraction(val_str: str):
    m = re.search(r"(\d+)\s*/\s*(\d+)", str(val_str or ""))
    if m:
        num, den = int(m.group(1)), int(m.group(2))
        if den >= 4:
            return num / den
    return None


async def enrich_with_sofascore(context, matches_list: list):
    if not matches_list:
        return
    sofa_page = await context.new_page()
    try:
        await sofa_page.goto("https://www.sofascore.com/", wait_until="domcontentloaded", timeout=30000)
        await sofa_page.wait_for_timeout(2000)

        now_utc = datetime.now(timezone.utc)
        date_strs = [(now_utc + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(0, 4)]
        sofa_events = []

        for ds in date_strs:
            for suffix in ("", "/inverse"):
                url = f"https://www.sofascore.com/api/v1/sport/football/scheduled-events/{ds}{suffix}"
                payload = await fetch_sofascore_json(sofa_page, url)
                if payload and isinstance(payload.get("events"), list):
                    sofa_events.extend(payload.get("events"))

        print(f"SofaScore loaded {len(sofa_events)} global + lower-league events.")
        detail_budget = 40

        for m in matches_list:
            en_h, en_a = m.get("enHome") or "", m.get("enAway") or ""
            if not en_h or not en_a:
                continue

            ko_str = m.get("kickoffIso") or ""
            ko_ts = None
            if ko_str:
                try:
                    ko_ts = datetime.fromisoformat(ko_str).timestamp()
                except ValueError:
                    ko_ts = None

            best_ev, best_score = None, 0.0
            for ev in sofa_events:
                sh = (ev.get("homeTeam") or {}).get("name") or ""
                sa = (ev.get("awayTeam") or {}).get("name") or ""
                ev_ts = ev.get("startTimestamp")
                if ko_ts and ev_ts and abs(ko_ts - float(ev_ts)) > 14 * 3600:
                    continue
                sim = (team_similarity(en_h, sh) + team_similarity(en_a, sa)) / 2.0
                if sim > best_score:
                    best_score = sim
                    best_ev = ev

            if best_ev and best_score >= 0.67:
                m["sofaMatched"] = True
                ev_id = best_ev.get("id")
                ev_ts = best_ev.get("startTimestamp")
                if ev_ts and not m.get("kickoffIso"):
                    m["kickoffIso"] = normalize_hkjc_kickoff(ev_ts)

                if detail_budget <= 0 or not ev_id:
                    continue
                detail_budget -= 1

                pf = await fetch_sofascore_json(sofa_page, f"https://www.sofascore.com/api/v1/event/{ev_id}/pregame-form")
                form_lambda = None
                if pf:
                    hr = float((pf.get("homeTeam") or {}).get("avgRating") or 0)
                    ar = float((pf.get("awayTeam") or {}).get("avgRating") or 0)
                    if hr > 5.5 and ar > 5.5:
                        form_lambda = round(2.45 + ((hr + ar) - 13.6) * 0.45, 2)

                st_data = await fetch_sofascore_json(sofa_page, f"https://www.sofascore.com/api/v1/event/{ev_id}/team-streaks")
                over25_rates = []
                if st_data:
                    for group_key in ("general", "head2head"):
                        for item in st_data.get(group_key) or []:
                            nm = str(item.get("name") or "").lower()
                            frac = parse_streak_fraction(item.get("value"))
                            if frac is not None:
                                if "more than 2.5" in nm:
                                    over25_rates.append(frac)
                                elif "less than 2.5" in nm:
                                    over25_rates.append(1.0 - frac)

                if over25_rates:
                    avg_over = sum(over25_rates) / len(over25_rates)
                    m["sofaOver25Rate"] = round(avg_over, 3)
                    p_u = min(0.88, max(0.12, 1.0 - avg_over))
                    lam_est = -math.log(p_u) * 1.85
                    form_lambda = round((form_lambda + lam_est) / 2.0, 2) if form_lambda else round(lam_est, 2)

                if form_lambda:
                    m["sofaFormLambda"] = max(1.45, min(4.10, form_lambda))

                lu = await fetch_sofascore_json(sofa_page, f"https://www.sofascore.com/api/v1/event/{ev_id}/lineups")
                if lu:
                    m["sofaLineupConfirmed"] = bool(lu.get("confirmed"))
                    m["sofaMissingHome"] = len((lu.get("home") or {}).get("missingPlayers") or [])
                    m["sofaMissingAway"] = len((lu.get("away") or {}).get("missingPlayers") or [])
    except Exception as exc:
        print(f"SofaScore enrichment warning: {exc}")
    finally:
        await sofa_page.close()


async def scrape_hkjc_and_sofascore():
    match_meta, kickoff_by_key, hilo_map, had_map = {}, {}, {}, {}
    dom_row_store = {}
    captured_matches = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="zh-HK",
            timezone_id="Asia/Hong_Kong",
            viewport={"width": 1440, "height": 2200},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        async def handle_response(response):
            if "graphql" in response.url.lower() and response.status == 200:
                try:
                    data = await response.json()
                    collect_hkjc_graphql(data, match_meta, kickoff_by_key, hilo_map, had_map)
                except Exception:
                    pass

        page.on("response", handle_response)

        # 第 1 步：載入足智彩中文「入球大細」網頁並逐段捲動擷取 GraphQL + DOM 列時間
        try:
            await page.goto(HKJC_HILO_CH_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            print(f"HKJC CH navigation notice: {exc}")
        await page.wait_for_timeout(5500)
        await scroll_and_harvest(page, dom_row_store, steps=6)

        # 第 2 步：快速開啟英文版足智彩「HiLo」補齊任何遺漏之開賽時間與官方英文隊名
        try:
            await page.goto(HKJC_HILO_EN_URL, wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_timeout(4000)
            await scroll_and_harvest(page, dom_row_store, steps=4)
        except Exception as exc:
            print(f"HKJC EN pass notice: {exc}")

        # 合併 GraphQL 與 DOM 列之開賽時間
        for key, meta in match_meta.items():
            fb_code = meta.get("code") or (key if key.startswith("FB") else "")
            odds_pair = hilo_map.get(key) or (hilo_map.get(fb_code) if fb_code else None)
            if fb_code and odds_pair and meta.get("home") and meta.get("away"):
                h_odds, l_odds = odds_pair
                had_tuple = had_map.get(key) or had_map.get(fb_code) or (None, None, None)
                had_h, had_d, had_a = had_tuple
                ko_final = (
                    meta.get("kickoffIso")
                    or kickoff_by_key.get(key)
                    or kickoff_by_key.get(fb_code)
                    or (dom_row_store.get(fb_code) or {}).get("kickoffIso")
                    or ""
                )
                captured_matches[fb_code] = {
                    "code": fb_code,
                    "league": meta.get("league") or "足智彩賽事",
                    "home": meta["home"],
                    "away": meta["away"],
                    "enHome": meta.get("enHome") or "",
                    "enAway": meta.get("enAway") or "",
                    "kickoffIso": ko_final,
                    "oddsHigh": h_odds,
                    "oddsLow": l_odds,
                    "hadHome": had_h,
                    "hadDraw": had_d,
                    "hadAway": had_a,
                    "sofaMatched": False,
                }

        # 純 DOM 補漏（若某場僅出現在網頁文字中）
        for fb_code, row_info in dom_row_store.items():
            r_txt = row_info.get("rowText") or ""
            ko_dom = row_info.get("kickoffIso") or ""
            if fb_code in captured_matches:
                if ko_dom and not captured_matches[fb_code].get("kickoffIso"):
                    captured_matches[fb_code]["kickoffIso"] = ko_dom
                if not captured_matches[fb_code].get("enHome"):
                    en_m = re.search(r"([A-Za-z][A-Za-z0-9\s\.\-]{2,28})\s+vs\s+([A-Za-z][A-Za-z0-9\s\.\-]{2,28})", r_txt, re.IGNORECASE)
                    if en_m:
                        captured_matches[fb_code]["enHome"] = en_m.group(1).strip()
                        captured_matches[fb_code]["enAway"] = en_m.group(2).strip()
            else:
                tm = re.search(r"([^\n\r\t]+?)\s*對\s*([^\n\r\t]+)", r_txt)
                om = re.search(r"(?:2\.5\s*)?(\d+\.\d{2})\s+(\d+\.\d{2})", r_txt)
                if tm and om:
                    captured_matches[fb_code] = {
                        "code": fb_code,
                        "league": "足智彩入球大細",
                        "home": clean_team_name(tm.group(1)),
                        "away": clean_team_name(tm.group(2)),
                        "enHome": "",
                        "enAway": "",
                        "kickoffIso": ko_dom,
                        "oddsHigh": float(om.group(1)),
                        "oddsLow": float(om.group(2)),
                        "sofaMatched": False,
                    }

        final_list = list(captured_matches.values())
        await enrich_with_sofascore(context, final_list)
        await browser.close()

    final_list.sort(key=lambda x: x.get("kickoffIso") or "9999-12-31T23:59:00+08:00")
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    if not final_list and os.path.exists(OUTPUT_PATH):
        print("HKJC maintenance window detected; keeping previous JSON snapshot.")
        return

    ko_ok_count = sum(1 for x in final_list if x.get("kickoffIso"))
    sofa_ok_count = sum(1 for x in final_list if x.get("sofaMatched"))
    output_payload = {
        "updatedAtHkt": datetime.now(HKT).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "matchCount": len(final_list),
        "kickoffResolvedCount": ko_ok_count,
        "sofaMatchedCount": sofa_ok_count,
        "matches": final_list,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(final_list)} HKJC matches (Kick-off times resolved: {ko_ok_count}, SofaScore enriched: {sofa_ok_count}).")


if __name__ == "__main__":
    asyncio.run(scrape_hkjc_and_sofascore())
