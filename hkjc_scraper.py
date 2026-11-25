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
HKJC_HAD_CH_URL = "https://bet.hkjc.com/ch/football/had"
HKJC_GQL_URL = "https://info.cld.hkjc.com/graphql/base/"
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

# 備援直接分頁 GraphQL 查詢（同時支援 startIndex / endIndex 分頁與多球線 HIL + HAD）
FALLBACK_PAGINATED_GQL = """
query matchList($startIndex: Int, $endIndex: Int, $startDate: String, $endDate: String, $fbOddsTypes: [FBOddsType]!, $fbOddsTypesM: [FBOddsType]!, $inplayOnly: Boolean) {
  matches(startIndex: $startIndex, endIndex: $endIndex, startDate: $startDate, endDate: $endDate, fbOddsTypes: $fbOddsTypes, fbOddsTypesM: $fbOddsTypesM, inplayOnly: $inplayOnly) {
    id
    frontEndId
    matchDate
    kickOffTime
    status
    homeTeam { name_en name_ch }
    awayTeam { name_en name_ch }
    tournament { name_en name_ch }
    foPools(fbOddsTypes: $fbOddsTypesM) {
      id
      oddsType
      status
      lines {
        lineId
        status
        condition
        main
        combinations {
          combId
          str
          status
          currentOdds
        }
      }
    }
  }
}
"""


def at(seq, idx, default=None):
    if not seq or idx < 0:
        return default
    return next(islice(seq, idx, None), default)


def load_previous_snapshot() -> dict:
    """讀取上一輪 data/hkjc_hilo.json，永久保留初盤 (openHigh/openLow)、前次賠率與賠率歷史軌跡"""
    prev_map = {}
    if not os.path.exists(OUTPUT_PATH):
        return prev_map
    try:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        items = raw if isinstance(raw, list) else (raw.get("matches") or [])
        for it in items:
            code = it.get("code")
            if code:
                prev_map[code] = it
    except Exception as exc:
        print(f"Previous snapshot read notice: {exc}")
    return prev_map


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
    if 0 <= hh_int <= 5:
        days_ahead = (days_ahead + 1) % 7
    cand = now_hkt + timedelta(days=days_ahead)
    return cand.strftime("%Y-%m-%d")


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
    s_norm = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)

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

    m_clock = re.search(r"(?<![\d:])(0?\d|1\d|2[0-3]):([0-5]\d)(?![\d])", s)
    if not m_clock:
        return ""
    hh_int, mn_str = int(m_clock.group(1)), m_clock.group(2)

    combo = f"{s} {fallback_date}"
    m_dmy = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", combo)
    if m_dmy:
        dd, mm, yyyy = m_dmy.groups()
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}T{hh_int:02d}:{mn_str}:00+08:00"

    m_ymd = re.search(r"(\d{4})-(\d{2})-(\d{2})", combo)
    if m_ymd:
        yyyy, mm, dd = m_ymd.groups()
        return f"{yyyy}-{mm}-{dd}T{hh_int:02d}:{mn_str}:00+08:00"

    m_zh = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日", combo)
    if m_zh:
        yyyy_raw, mm, dd = m_zh.groups()
        mm_int = int(mm)
        yyyy = int(yyyy_raw) if yyyy_raw else resolve_year_for_month(mm_int)
        return f"{yyyy}-{mm_int:02d}-{int(dd):02d}T{hh_int:02d}:{mn_str}:00+08:00"

    for m_dm in re.finditer(r"(?<![\.\d])(\d{1,2})/(\d{1,2})(?![/\.\d])", combo):
        dd_int, mm_int = int(m_dm.group(1)), int(m_dm.group(2))
        if 1 <= dd_int <= 31 and 1 <= mm_int <= 12:
            yyyy = resolve_year_for_month(mm_int)
            return f"{yyyy}-{mm_int:02d}-{dd_int:02d}T{hh_int:02d}:{mn_str}:00+08:00"

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
    for key in ("frontEndId", "code", "fbId", "matchCode", "id"):
        val = str(node.get(key) or "").strip()
        m = re.search(r"(FB\d{4})", val)
        if m:
            return m.group(1)
    for val in node.values():
        if isinstance(val, str) and "FB" in val:
            m = re.search(r"(FB\d{4})", val)
            if m:
                return m.group(1)
    return ""


def parse_goal_line_condition(cond_raw):
    """解析馬會所有入球大細球線（例如 '2.5', '2.0/2.5', '2.5/3.0', '3.0/3.5', '1.5/2.0'），回傳 (數值, 顯示標籤)"""
    s = str(cond_raw or "").strip()
    if not s:
        return None, ""
    parts = [p.strip() for p in s.split("/") if p.strip()]
    try:
        vals = [float(p) for p in parts]
        avg_line = round(sum(vals) / len(vals), 2)
        if 1.5 <= avg_line <= 4.5:
            if len(vals) == 2 and abs(at(vals, 0) - at(vals, 1)) > 0.05:
                label = f"{at(vals, 0):.1f}/{at(vals, 1):.1f}"
            else:
                label = f"{avg_line:g}"
                if "." not in label:
                    label = f"{avg_line:.1f}"
            return avg_line, label
    except ValueError:
        pass
    return None, ""


def is_status_started_or_ended(node: dict) -> bool:
    st = str(node.get("status") or node.get("matchStatus") or node.get("matchState") or "").strip().upper()
    bad_set = {
        "INPLAY", "IN_PLAY", "STARTED", "FIRSTHALF", "SECONDHALF",
        "HALFTIME", "COMPLETED", "ENDED", "CANCELLED", "ABANDONED", "CLOSED",
    }
    return st in bad_set


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


def choose_best_hil_line(candidates: list):
    """優先選取純 2.5 球線；若馬會該場僅開 2.0/2.5 或 2.5/3.0 等球線，自動選最接近 2.5 之正盤而不漏場"""
    if not candidates:
        return None
    candidates_sorted = sorted(
        candidates,
        key=lambda x: (0 if abs(x.get("goalLine", 0) - 2.5) < 0.01 else 1, abs(x.get("goalLine", 0) - 2.5), 0 if x.get("isMain") else 1),
    )
    return at(candidates_sorted, 0)


def collect_hkjc_graphql(
    payload,
    match_meta: dict,
    kickoff_by_key: dict,
    hilo_candidates_map: dict,
    had_map: dict,
    started_keys: set,
    parent_id: str = "",
    parent_fb: str = "",
):
    if isinstance(payload, dict):
        own_fb = extract_fb_code(payload)
        is_match_node = isinstance(payload.get("homeTeam"), dict) and isinstance(payload.get("awayTeam"), dict)

        cur_fb = own_fb or parent_fb
        cur_id = parent_id
        if is_match_node:
            cur_id = str(payload.get("id") or payload.get("matchId") or own_fb or parent_id or "").strip()
            if is_status_started_or_ended(payload):
                for k in (cur_id, cur_fb):
                    if k:
                        started_keys.add(k)

        ko_here = scan_nested_kickoff(payload, str(payload.get("matchDate") or payload.get("frontEndId") or ""))
        if ko_here:
            for k in (cur_id, cur_fb, str(payload.get("id") or "")):
                if k:
                    kickoff_by_key[k] = ko_here

        if is_match_node:
            h_obj, a_obj = payload.get("homeTeam") or {}, payload.get("awayTeam") or {}
            t_obj = payload.get("tournament") or {}
            ko_iso = ko_here or kickoff_by_key.get(cur_id) or kickoff_by_key.get(cur_fb) or ""
            prev = match_meta.get(cur_fb or cur_id, {})
            meta = {
                "id": cur_id,
                "code": cur_fb or prev.get("code") or "",
                "league": t_obj.get("name_ch") or prev.get("league") or t_obj.get("name_en") or "足智彩賽事",
                "home": clean_team_name(h_obj.get("name_ch") or prev.get("home") or h_obj.get("name_en") or ""),
                "away": clean_team_name(a_obj.get("name_ch") or prev.get("away") or a_obj.get("name_en") or ""),
                "enHome": h_obj.get("name_en") or prev.get("enHome") or "",
                "enAway": a_obj.get("name_en") or prev.get("enAway") or "",
                "kickoffIso": ko_iso or prev.get("kickoffIso") or "",
            }
            for k in (cur_id, cur_fb):
                if k:
                    match_meta[k] = meta

        odds_type = str(payload.get("oddsType") or "").upper()
        owner_keys = [str(payload.get("matchId") or ""), cur_fb, cur_id]

        if odds_type in ("HIL", "CHL"):
            if odds_type == "HIL":
                for line in payload.get("lines") or []:
                    line_st = str(line.get("status") or "AVAILABLE").upper()
                    if line_st in ("CLOSED", "SUSPENDED", "UNAVAILABLE"):
                        continue
                    g_val, g_label = parse_goal_line_condition(line.get("condition"))
                    if g_val is None:
                        continue
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
                    if h_val and l_val and 1.15 <= h_val <= 5.20 and 1.15 <= l_val <= 5.20:
                        cand_obj = {
                            "goalLine": g_val,
                            "lineLabel": g_label,
                            "oddsHigh": h_val,
                            "oddsLow": l_val,
                            "isMain": bool(line.get("main")),
                        }
                        for ok in owner_keys:
                            if ok:
                                lst = hilo_candidates_map.setdefault(ok, [])
                                lst = [x for x in lst if x.get("lineLabel") != g_label]
                                lst.append(cand_obj)
                                hilo_candidates_map[ok] = lst

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
            collect_hkjc_graphql(v, match_meta, kickoff_by_key, hilo_candidates_map, had_map, started_keys, cur_id, cur_fb)
    elif isinstance(payload, list):
        for item in payload:
            collect_hkjc_graphql(item, match_meta, kickoff_by_key, hilo_candidates_map, had_map, started_keys, parent_id, parent_fb)


async def replay_intercepted_pagination(context, captured_gql_reqs: list, match_meta, kickoff_by_key, hilo_candidates_map, had_map, started_keys):
    """重播馬會真實發出的 GraphQL 請求並自動延伸分頁 (1..300) 與 8 天日期範圍，抓齊全部賽事"""
    now_hkt = datetime.now(HKT)
    start_str = now_hkt.strftime("%Y-%m-%d")
    end_str = (now_hkt + timedelta(days=8)).strftime("%Y-%m-%d")

    # 1. 若攔截到馬會前端簽發之真實 GraphQL 查詢範本，沿用其查詢結構進行分頁擴展
    for req_tpl in captured_gql_reqs[:3]:
        q_str = req_tpl.get("query") or ""
        base_vars = dict(req_tpl.get("variables") or {})
        hdrs = dict(req_tpl.get("headers") or {})
        url = req_tpl.get("url") or HKJC_GQL_URL
        if "matches" not in q_str and "foPools" not in q_str:
            continue

        page_ranges = [(1, 100), (101, 200), (201, 300)]
        for s_idx, e_idx in page_ranges:
            vars_copy = dict(base_vars)
            if "startIndex" in vars_copy or "startIndex" in q_str:
                vars_copy["startIndex"] = s_idx
                vars_copy["endIndex"] = e_idx
            if "startDate" in vars_copy:
                vars_copy["startDate"] = start_str
            if "endDate" in vars_copy:
                vars_copy["endDate"] = end_str
            if "inplayOnly" in vars_copy:
                vars_copy["inplayOnly"] = False
            try:
                clean_hdrs = {
                    k: v for k, v in hdrs.items()
                    if k.lower() not in ("content-length", "host", "connection")
                }
                resp = await context.request.post(
                    url,
                    headers=clean_hdrs,
                    data=json.dumps({"query": q_str, "variables": vars_copy}),
                    timeout=15000,
                )
                if resp.status == 200:
                    data = await resp.json()
                    collect_hkjc_graphql(data, match_meta, kickoff_by_key, hilo_candidates_map, had_map, started_keys)
            except Exception:
                continue

    # 2. 同時執行標準 1..300 分頁 GraphQL 補齊
    for s_idx, e_idx in [(1, 100), (101, 200), (201, 300)]:
        variables = {
            "startIndex": s_idx,
            "endIndex": e_idx,
            "startDate": start_str,
            "endDate": end_str,
            "fbOddsTypes": ["HIL", "HAD"],
            "fbOddsTypesM": ["HIL", "HAD"],
            "inplayOnly": False,
        }
        try:
            resp = await context.request.post(
                HKJC_GQL_URL,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Origin": "https://bet.hkjc.com",
                    "Referer": "https://bet.hkjc.com/",
                },
                data=json.dumps({"query": FALLBACK_PAGINATED_GQL, "variables": variables}),
                timeout=15000,
            )
            if resp.status == 200:
                data = await resp.json()
                collect_hkjc_graphql(data, match_meta, kickoff_by_key, hilo_candidates_map, had_map, started_keys)
        except Exception:
            continue


async def harvest_dom_rows_at_scroll(page, dom_row_store: dict):
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
                    if (fbMatches && fbMatches.length === 1 && txt.length >= 16 && txt.length <= 650) {
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
            r_txt = r.get("rowText") or ""
            live_clock_dom = bool(re.search(r"(上半場|下半場|半場|加時|\bHT\b|\d{1,2}\'\s)", r_txt, re.IGNORECASE))
            ko = normalize_hkjc_kickoff(r_txt, r.get("bannerDate") or "")
            existing = dom_row_store.get(code, {})
            dom_row_store[code] = {
                "rowText": r_txt if len(r_txt) >= len(existing.get("rowText") or "") else existing.get("rowText"),
                "kickoffIso": ko or existing.get("kickoffIso") or "",
                "isLiveDom": live_clock_dom or bool(existing.get("isLiveDom")),
            }
    except Exception:
        pass


async def expand_and_scroll_full_page(page, dom_row_store: dict, max_steps: int = 28):
    """自動展開折疊面板並深度捲動最多 28 步，直至頁面底部所有馬會賽事全數載入"""
    try:
        await page.evaluate(
            """() => {
                document.querySelectorAll('button, div, span').forEach(el => {
                    const t = (el.innerText || '').trim();
                    if (t === '顯示更多' || t === '更多賽事' || t === '全部顯示') {
                        try { el.click(); } catch (_) {}
                    }
                });
            }"""
        )
    except Exception:
        pass

    await harvest_dom_rows_at_scroll(page, dom_row_store)
    stagnant_rounds = 0
    prev_count = len(dom_row_store)

    for _ in range(max_steps):
        try:
            await page.evaluate(
                """() => {
                    window.scrollBy(0, 1300);
                    document.querySelectorAll('div, main, section').forEach(el => {
                        if (el.scrollHeight > el.clientHeight + 250) el.scrollTop += 1300;
                    });
                }"""
            )
            await page.wait_for_timeout(750)
            await harvest_dom_rows_at_scroll(page, dom_row_store)
            cur_count = len(dom_row_store)
            if cur_count == prev_count:
                stagnant_rounds += 1
                if stagnant_rounds >= 5:
                    break
            else:
                stagnant_rounds = 0
                prev_count = cur_count
        except Exception:
            break


async def fetch_sofa_dual_host(context, path_part: str):
    for host in ("https://api.sofascore.com", "https://www.sofascore.com"):
        try:
            resp = await context.request.get(
                f"{host}{path_part}",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://www.sofascore.com/",
                    "Origin": "https://www.sofascore.com",
                },
                timeout=7000,
            )
            if resp.status == 200:
                return await resp.json()
        except Exception:
            continue
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
        try:
            await sofa_page.goto("https://www.sofascore.com/", wait_until="domcontentloaded", timeout=15000)
            await sofa_page.wait_for_timeout(1000)
        except Exception:
            pass

        now_utc = datetime.now(timezone.utc)
        date_strs = [(now_utc + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(0, 5)]
        schedule_paths = [
            f"/api/v1/sport/football/scheduled-events/{ds}{suffix}"
            for ds in date_strs
            for suffix in ("", "/inverse")
        ]
        sched_results = await asyncio.gather(*(fetch_sofa_dual_host(context, p) for p in schedule_paths))
        sofa_events = []
        for payload in sched_results:
            if payload and isinstance(payload.get("events"), list):
                sofa_events.extend(payload.get("events"))

        if not sofa_events:
            return

        sem = asyncio.Semaphore(6)

        async def enrich_single_match(m: dict, ev_id: int):
            async with sem:
                pf, st_data = await asyncio.gather(
                    fetch_sofa_dual_host(context, f"/api/v1/event/{ev_id}/pregame-form"),
                    fetch_sofa_dual_host(context, f"/api/v1/event/{ev_id}/team-streaks"),
                )
                form_lambda = None
                if pf:
                    hr = float((pf.get("homeTeam") or {}).get("avgRating") or 0)
                    ar = float((pf.get("awayTeam") or {}).get("avgRating") or 0)
                    if hr > 5.5 and ar > 5.5:
                        form_lambda = round(2.45 + ((hr + ar) - 13.6) * 0.45, 2)

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
                    m["sofaFormLambda"] = max(1.45, min(4.15, form_lambda))

        tasks = []
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
                if ev_id and len(tasks) < 55:
                    tasks.append(enrich_single_match(m, ev_id))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as exc:
        print(f"SofaScore enrichment notice: {exc}")
    finally:
        await sofa_page.close()


def attach_persistent_odds_history(m: dict, prev_snapshot: dict, now_short_hkt: str):
    """將最新馬會賠率與上一輪檔案比對，永久保存初盤 (openHigh/openLow)、上次變動盤與賠率落飛 %"""
    code = m["code"]
    cur_h = float(m["oddsHigh"])
    cur_l = float(m["oddsLow"])
    cur_label = m.get("lineLabel") or "2.5"

    old = prev_snapshot.get(code)
    # 若球線相同，沿用歷史初盤；若馬會轉線則以新球線首次賠率為初盤
    if old and str(old.get("lineLabel") or "2.5") == str(cur_label):
        open_h = float(old.get("openHigh") or old.get("oddsHigh") or cur_h)
        open_l = float(old.get("openLow") or old.get("oddsLow") or cur_l)
        old_cur_h = float(old.get("oddsHigh") or cur_h)
        old_cur_l = float(old.get("oddsLow") or cur_l)
        if abs(cur_h - old_cur_h) >= 0.01 or abs(cur_l - old_cur_l) >= 0.01:
            prev_h, prev_l = old_cur_h, old_cur_l
        else:
            prev_h = float(old.get("prevHigh") or open_h)
            prev_l = float(old.get("prevLow") or open_l)

        hist = list(old.get("oddsHistory") or [])
        if not hist:
            hist.append({"t": "初盤", "h": open_h, "l": open_l})
        last_entry = at(hist, len(hist) - 1, {})
        if abs(float(last_entry.get("h") or 0) - cur_h) >= 0.01 or abs(float(last_entry.get("l") or 0) - cur_l) >= 0.01:
            hist.append({"t": now_short_hkt, "h": cur_h, "l": cur_l})
        hist = hist[-8:]
    else:
        open_h, open_l = cur_h, cur_l
        prev_h, prev_l = cur_h, cur_l
        hist = [{"t": f"初盤 ({now_short_hkt})", "h": cur_h, "l": cur_l}]

    m["openHigh"] = round(open_h, 2)
    m["openLow"] = round(open_l, 2)
    m["prevHigh"] = round(prev_h, 2)
    m["prevLow"] = round(prev_l, 2)
    m["dropHighPct"] = round(((open_h - cur_h) / open_h) * 100.0, 2) if open_h > 0 else 0.0
    m["dropLowPct"] = round(((open_l - cur_l) / open_l) * 100.0, 2) if open_l > 0 else 0.0
    m["oddsHistory"] = hist


async def scrape_hkjc_and_sofascore():
    prev_snapshot = load_previous_snapshot()
    match_meta, kickoff_by_key, hilo_candidates_map, had_map = {}, {}, {}, {}
    started_keys = set()
    dom_row_store = {}
    captured_gql_reqs = []
    captured_matches = {}
    now_dt = datetime.now(HKT)
    now_ts = now_dt.timestamp()
    now_short_hkt = now_dt.strftime("%d/%m %H:%M")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            locale="zh-HK",
            timezone_id="Asia/Hong_Kong",
            viewport={"width": 1440, "height": 2400},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = await context.new_page()

        def handle_request(request):
            if "graphql" in request.url.lower() and request.method.upper() == "POST":
                try:
                    post_json = json.loads(request.post_data or "{}")
                    if isinstance(post_json, dict) and post_json.get("query"):
                        captured_gql_reqs.append({
                            "url": request.url,
                            "headers": request.headers,
                            "query": post_json.get("query"),
                            "variables": post_json.get("variables") or {},
                        })
                except Exception:
                    pass

        async def handle_response(response):
            if "graphql" in response.url.lower() and response.status == 200:
                try:
                    data = await response.json()
                    collect_hkjc_graphql(data, match_meta, kickoff_by_key, hilo_candidates_map, had_map, started_keys)
                except Exception:
                    pass

        page.on("request", handle_request)
        page.on("response", handle_response)

        try:
            await page.goto(HKJC_HILO_CH_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            print(f"HKJC CH navigation notice: {exc}")

        for _ in range(12):
            if hilo_candidates_map:
                break
            await page.wait_for_timeout(1000)
        await page.wait_for_timeout(2000)
        await expand_and_scroll_full_page(page, dom_row_store, max_steps=28)

        # 重播攔截到的 GraphQL 請求與 1..300 分頁查詢，確保遠期與次級賽事無遺漏
        await replay_intercepted_pagination(
            context, captured_gql_reqs, match_meta, kickoff_by_key, hilo_candidates_map, had_map, started_keys
        )

        # 順道讀取 HAD 主客和頁面以補齊主客和賠率與遺漏場次
        try:
            await page.goto(HKJC_HAD_CH_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2500)
        except Exception:
            pass

        try:
            await page.goto(HKJC_HILO_EN_URL, wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_timeout(2500)
            await expand_and_scroll_full_page(page, dom_row_store, max_steps=14)
        except Exception as exc:
            print(f"HKJC EN pass notice: {exc}")

        for key, meta in match_meta.items():
            fb_code = meta.get("code") or (key if key.startswith("FB") else "")
            if not fb_code:
                continue
            if (key in started_keys) or (fb_code in started_keys):
                continue
            if (dom_row_store.get(fb_code) or {}).get("isLiveDom"):
                continue

            cands = hilo_candidates_map.get(key) or hilo_candidates_map.get(fb_code) or []
            best_line = choose_best_hil_line(cands)
            if best_line and meta.get("home") and meta.get("away"):
                ko_final = (
                    meta.get("kickoffIso")
                    or kickoff_by_key.get(key)
                    or kickoff_by_key.get(fb_code)
                    or (dom_row_store.get(fb_code) or {}).get("kickoffIso")
                    or ""
                )
                if ko_final:
                    try:
                        if datetime.fromisoformat(ko_final).timestamp() <= (now_ts - 300):
                            continue
                    except ValueError:
                        pass

                had_tuple = had_map.get(key) or had_map.get(fb_code) or (None, None, None)
                had_h, had_d, had_a = had_tuple
                captured_matches[fb_code] = {
                    "code": fb_code,
                    "league": meta.get("league") or "足智彩賽事",
                    "home": meta["home"],
                    "away": meta["away"],
                    "enHome": meta.get("enHome") or "",
                    "enAway": meta.get("enAway") or "",
                    "kickoffIso": ko_final,
                    "goalLine": best_line["goalLine"],
                    "lineLabel": best_line["lineLabel"],
                    "oddsHigh": best_line["oddsHigh"],
                    "oddsLow": best_line["oddsLow"],
                    "hadHome": had_h,
                    "hadDraw": had_d,
                    "hadAway": had_a,
                    "isInPlay": False,
                    "sofaMatched": False,
                }

        # DOM 補漏：支援 2.5、2.0/2.5、2.5/3.0、3.0/3.5、1.5/2.0 等所有球線
        dom_line_re = re.compile(r"((?:1\.5|2\.0|2\.5|3\.0|3\.5)(?:/(?:2\.0|2\.5|3\.0|3\.5|4\.0))?)(?:\s*球)?[^\d]{0,10}(\d+\.\d{2})\s+(\d+\.\d{2})")
        for fb_code, row_info in dom_row_store.items():
            if row_info.get("isLiveDom") or (fb_code in started_keys):
                continue
            r_txt = row_info.get("rowText") or ""
            ko_dom = row_info.get("kickoffIso") or ""
            if ko_dom:
                try:
                    if datetime.fromisoformat(ko_dom).timestamp() <= (now_ts - 300):
                        continue
                except ValueError:
                    pass

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
                line_hits = []
                for lm in dom_line_re.finditer(r_txt):
                    g_val, g_label = parse_goal_line_condition(lm.group(1))
                    h_o, l_o = float(lm.group(2)), float(lm.group(3))
                    if g_val and 1.15 <= h_o <= 5.20 and 1.15 <= l_o <= 5.20:
                        line_hits.append({"goalLine": g_val, "lineLabel": g_label, "oddsHigh": h_o, "oddsLow": l_o, "isMain": True})
                best_dom = choose_best_hil_line(line_hits)
                if tm and best_dom:
                    captured_matches[fb_code] = {
                        "code": fb_code,
                        "league": "足智彩入球大細",
                        "home": clean_team_name(tm.group(1)),
                        "away": clean_team_name(tm.group(2)),
                        "enHome": "",
                        "enAway": "",
                        "kickoffIso": ko_dom,
                        "goalLine": best_dom["goalLine"],
                        "lineLabel": best_dom["lineLabel"],
                        "oddsHigh": best_dom["oddsHigh"],
                        "oddsLow": best_dom["oddsLow"],
                        "isInPlay": False,
                        "sofaMatched": False,
                    }

        final_list = list(captured_matches.values())
        print(f"HKJC full pre-match slate captured: {len(final_list)} matches.")

        # 合併上一輪初盤與賠率變動歷史（同時保留 SofaScore 快取避免重複請求）
        for m in final_list:
            old_rec = prev_snapshot.get(m["code"]) or {}
            if old_rec.get("sofaMatched") and not m.get("sofaMatched"):
                m["sofaMatched"] = True
                m["sofaFormLambda"] = old_rec.get("sofaFormLambda")
                m["sofaOver25Rate"] = old_rec.get("sofaOver25Rate")
            attach_persistent_odds_history(m, prev_snapshot, now_short_hkt)

        try:
            await asyncio.wait_for(enrich_with_sofascore(context, final_list), timeout=42.0)
        except asyncio.TimeoutError:
            print("SofaScore enrichment reached 42s cap; proceeding with full HKJC slate.")

        await browser.close()

    final_list = [
        m for m in final_list
        if not m.get("kickoffIso") or datetime.fromisoformat(m["kickoffIso"]).timestamp() > (now_ts - 300)
    ]
    final_list.sort(key=lambda x: x.get("kickoffIso") or "9999-12-31T23:59:00+08:00")
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    ko_ok_count = sum(1 for x in final_list if x.get("kickoffIso"))
    moved_count = sum(
        1 for x in final_list
        if abs(x.get("dropHighPct") or 0) >= 0.5 or abs(x.get("dropLowPct") or 0) >= 0.5
    )

    output_payload = {
        "updatedAtHkt": datetime.now(HKT).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "matchCount": len(final_list),
        "kickoffResolvedCount": ko_ok_count,
        "oddsMovedCount": moved_count,
        "matches": final_list,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(final_list)} HKJC matches (Kickoff resolved: {ko_ok_count}, Odds moved: {moved_count}).")


if __name__ == "__main__":
    asyncio.run(scrape_hkjc_and_sofascore())
