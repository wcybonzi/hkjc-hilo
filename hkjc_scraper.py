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
HKJC_HILO_URL = "https://bet.hkjc.com/ch/football/hil"
OUTPUT_PATH = "data/hkjc_hilo.json"


def at(seq, idx, default=None):
    if not seq or idx < 0:
        return default
    return next(islice(seq, idx, None), default)


def normalize_hkjc_kickoff(raw_val, fallback_date="") -> str:
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

    if re.search(r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}", s):
        try:
            iso_clean = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_clean)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=HKT)
            return dt.astimezone(HKT).strftime("%Y-%m-%dT%H:%M:00+08:00")
        except ValueError:
            m_iso = re.search(r"(\d{4})-(\d{2})-(\d{2})[T\s]+(\d{2}):(\d{2})", s)
            if m_iso:
                yyyy, mm, dd, hh, mn = m_iso.groups()
                return f"{yyyy}-{mm}-{dd}T{hh}:{mn}:00+08:00"

    m_dmy = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})[^\d]+(\d{1,2}):(\d{2})", s)
    if m_dmy:
        dd, mm, yyyy, hh, mn = m_dmy.groups()
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}T{int(hh):02d}:{mn}:00+08:00"

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

    m_hm = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", s)
    if m_hm and fallback_date:
        hh, mn = m_hm.groups()
        m_fd = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(fallback_date))
        if m_fd:
            yyyy, mm, dd = m_fd.groups()
            return f"{yyyy}-{mm}-{dd}T{int(hh):02d}:{mn}:00+08:00"
    return ""


def scan_nested_kickoff(obj, fallback_date="") -> str:
    if isinstance(obj, dict):
        priority_keys = ("kickOffTime", "kickoffTime", "startTime", "matchTime", "esst", "kickOff")
        for pk in priority_keys:
            if pk in obj:
                ko = normalize_hkjc_kickoff(obj.get(pk), fallback_date)
                if ko:
                    return ko
        for k, v in obj.items():
            if k in ("foPools", "lines", "combinations"):
                continue
            ko = scan_nested_kickoff(v, fallback_date)
            if ko:
                return ko
    elif isinstance(obj, list):
        for item in obj:
            ko = scan_nested_kickoff(item, fallback_date)
            if ko:
                return ko
    elif isinstance(obj, str) and len(obj) >= 11:
        return normalize_hkjc_kickoff(obj, fallback_date)
    return ""


def extract_fb_code(node: dict) -> str:
    for key in ("frontEndId", "fbId", "matchCode", "code", "id"):
        val = str(node.get(key) or "").strip()
        m = re.search(r"(FB\d{4})", val)
        if m:
            return m.group(1)
    return ""


def clean_team_name(name: str) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"\d{1,2}/\d{1,2}(?:/\d{4})?\s*(?:\([周星期一二三四五六日]+\))?\s*\d{1,2}:\d{2}", "", name)
    return cleaned.strip()


def norm_en_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9\s]", " ", str(name or "").lower())
    stopwords = {"fc", "sc", "cf", "ac", "afc", "club", "de", "al", "united", "utd", "city", "sporting", "utd"}
    tokens = [t for t in s.split() if t not in stopwords and len(t) > 1]
    return " ".join(tokens) if tokens else s.strip()


def team_similarity(a: str, b: str) -> float:
    na, nb = norm_en_name(a), norm_en_name(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 0.92
    return SequenceMatcher(None, na, nb).ratio()


def collect_hkjc_graphql(payload, match_meta: dict, hilo_map: dict, had_map: dict):
    if isinstance(payload, dict):
        fb_code = extract_fb_code(payload)
        raw_id = str(payload.get("id") or fb_code or "").strip()
        if isinstance(payload.get("homeTeam"), dict) and isinstance(payload.get("awayTeam"), dict):
            h_obj, a_obj = payload.get("homeTeam") or {}, payload.get("awayTeam") or {}
            t_obj = payload.get("tournament") or {}
            ko_iso = scan_nested_kickoff(payload, str(payload.get("matchDate") or ""))
            meta = {
                "id": raw_id,
                "code": fb_code,
                "league": t_obj.get("name_ch") or t_obj.get("name_en") or "足智彩賽事",
                "home": clean_team_name(h_obj.get("name_ch") or h_obj.get("name_en") or ""),
                "away": clean_team_name(a_obj.get("name_ch") or a_obj.get("name_en") or ""),
                "enHome": h_obj.get("name_en") or "",
                "enAway": a_obj.get("name_en") or "",
                "kickoffIso": ko_iso,
            }
            for k in (raw_id, fb_code):
                if k:
                    prev = match_meta.get(k, {})
                    if not meta["kickoffIso"] and prev.get("kickoffIso"):
                        meta["kickoffIso"] = prev["kickoffIso"]
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
            collect_hkjc_graphql(v, match_meta, hilo_map, had_map)
    elif isinstance(payload, list):
        for item in payload:
            collect_hkjc_graphql(item, match_meta, hilo_map, had_map)


async def fetch_sofascore_json(page, url: str):
    try:
        data = await page.evaluate(
            """async (targetUrl) => {
                const r = await fetch(targetUrl, { headers: { 'Accept': 'application/json' } });
                if (!r.ok) return null;
                return await r.json();
            }""",
            url,
        )
        return data
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
    """使用 Playwright 瀏覽器連線 SofaScore（含 /inverse 次級聯賽賽程、近況評分、2.5大細走勢及傷停正選）"""
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

        print(f"SofaScore loaded {len(sofa_events)} global + lower-league events across 4 days.")
        detail_budget = 40  # 控制每次雲端執行之細項查詢上限，確保 90 秒內穩定完成

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
                if ko_ts and ev_ts and abs(ko_ts - float(ev_ts)) > 12 * 3600:
                    continue
                sim = (team_similarity(en_h, sh) + team_similarity(en_a, sa)) / 2.0
                if sim > best_score:
                    best_score = sim
                    best_ev = ev

            if best_ev and best_score >= 0.68:
                m["sofaMatched"] = True
                ev_id = best_ev.get("id")
                ev_ts = best_ev.get("startTimestamp")
                if ev_ts and not m.get("kickoffIso"):
                    m["kickoffIso"] = normalize_hkjc_kickoff(ev_ts)

                if detail_budget <= 0 or not ev_id:
                    continue
                detail_budget -= 1

                # 1. 賽前近況評分與場均表現 (/pregame-form)
                pf = await fetch_sofascore_json(sofa_page, f"https://www.sofascore.com/api/v1/event/{ev_id}/pregame-form")
                form_lambda = None
                if pf:
                    hr = float((pf.get("homeTeam") or {}).get("avgRating") or 0)
                    ar = float((pf.get("awayTeam") or {}).get("avgRating") or 0)
                    if hr > 5.5 and ar > 5.5:
                        form_lambda = round(2.45 + ((hr + ar) - 13.6) * 0.45, 2)

                # 2. 兩隊近期 2.5 入球大細真實走勢 (/team-streaks)
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
                    # 由真實大球率換算泊松隱含 Lambda 並與近況融合
                    p_u = min(0.88, max(0.12, 1.0 - avg_over))
                    lam_est = -math.log(p_u) * 1.85
                    form_lambda = round((form_lambda + lam_est) / 2.0, 2) if form_lambda else round(lam_est, 2)

                if form_lambda:
                    m["sofaFormLambda"] = max(1.45, min(4.10, form_lambda))

                # 3. 傷停缺陣人數與 T-60m 官方正選確認 (/lineups)
                lu = await fetch_sofascore_json(sofa_page, f"https://www.sofascore.com/api/v1/event/{ev_id}/lineups")
                if lu:
                    m["sofaLineupConfirmed"] = bool(lu.get("confirmed"))
                    h_miss = len((lu.get("home") or {}).get("missingPlayers") or [])
                    a_miss = len((lu.get("away") or {}).get("missingPlayers") or [])
                    m["sofaMissingHome"] = h_miss
                    m["sofaMissingAway"] = a_miss
    except Exception as exc:
        print(f"SofaScore enrichment warning (HKJC odds still safe): {exc}")
    finally:
        await sofa_page.close()


async def scrape_hkjc_and_sofascore():
    match_meta, hilo_map, had_map = {}, {}, {}
    captured_matches = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="zh-HK",
            timezone_id="Asia/Hong_Kong",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        async def handle_response(response):
            if "graphql" in response.url.lower() and response.status == 200:
                try:
                    data = await response.json()
                    collect_hkjc_graphql(data, match_meta, hilo_map, had_map)
                except Exception:
                    pass

        page.on("response", handle_response)
        try:
            await page.goto(HKJC_HILO_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            print(f"HKJC navigation notice: {exc}")

        await page.wait_for_timeout(6000)
        for _ in range(4):
            try:
                await page.evaluate("window.scrollBy(0, 1200)")
                await page.wait_for_timeout(1100)
            except Exception:
                break

        for key, meta in match_meta.items():
            fb_code = meta.get("code") or (key if key.startswith("FB") else "")
            odds_pair = hilo_map.get(key) or (hilo_map.get(fb_code) if fb_code else None)
            if fb_code and odds_pair and meta.get("home") and meta.get("away"):
                h_odds, l_odds = odds_pair
                had_tuple = had_map.get(key) or had_map.get(fb_code) or (None, None, None)
                had_h, had_d, had_a = had_tuple
                captured_matches[fb_code] = {
                    "code": fb_code,
                    "league": meta.get("league") or "足智彩賽事",
                    "home": meta["home"],
                    "away": meta["away"],
                    "enHome": meta.get("enHome") or "",
                    "enAway": meta.get("enAway") or "",
                    "kickoffIso": meta.get("kickoffIso") or "",
                    "oddsHigh": h_odds,
                    "oddsLow": l_odds,
                    "hadHome": had_h,
                    "hadDraw": had_d,
                    "hadAway": had_a,
                    "sofaMatched": False,
                }

        # 網頁文字雙向補漏（同時補齊開賽時間）
        try:
            body_text = await page.inner_text("body")
            for m_fb in re.finditer(r"(FB\d{4})", body_text):
                code = m_fb.group(1)
                start_pos = max(0, m_fb.start() - 100)
                window = body_text[start_pos : m_fb.start() + 420]
                ko_found = normalize_hkjc_kickoff(window)
                if code in captured_matches and ko_found and not captured_matches[code].get("kickoffIso"):
                    captured_matches[code]["kickoffIso"] = ko_found
                elif code not in captured_matches:
                    after_txt = body_text[m_fb.start() : m_fb.start() + 380]
                    tm = re.search(r"([^\n\r\t]+?)\s*對\s*([^\n\r\t]+)", after_txt)
                    om = re.search(r"\[?2\.5\]?\s*(\d+\.\d{2})\s+(\d+\.\d{2})", after_txt)
                    if tm and om:
                        captured_matches[code] = {
                            "code": code,
                            "league": "足智彩入球大細",
                            "home": clean_team_name(tm.group(1)),
                            "away": clean_team_name(tm.group(2)),
                            "enHome": "",
                            "enAway": "",
                            "kickoffIso": ko_found,
                            "oddsHigh": float(om.group(1)),
                            "oddsLow": float(om.group(2)),
                            "sofaMatched": False,
                        }
        except Exception as exc:
            print(f"DOM fallback warning: {exc}")

        final_list = list(captured_matches.values())
        # 連線 SofaScore 補齊次級聯賽近況、2.5大細走勢、傷停與正選
        await enrich_with_sofascore(context, final_list)
        await browser.close()

    final_list.sort(key=lambda x: x.get("kickoffIso") or "9999-12-31T23:59:00+08:00")
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    if not final_list and os.path.exists(OUTPUT_PATH):
        print("HKJC maintenance window detected; keeping previous JSON snapshot.")
        return

    output_payload = {
        "updatedAtHkt": datetime.now(HKT).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "matchCount": len(final_list),
        "sofaMatchedCount": sum(1 for x in final_list if x.get("sofaMatched")),
        "matches": final_list,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(final_list)} HKJC matches ({output_payload['sofaMatchedCount']} enriched via SofaScore).")


if __name__ == "__main__":
    asyncio.run(scrape_hkjc_and_sofascore())
