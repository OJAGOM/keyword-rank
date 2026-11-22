"""네이버 쇼핑 키워드별 순위 체크

- 네이버 쇼핑 검색 API(정확도순)에서 config.json 의 상품이 몇 위인지 찾는다.
- 결과를 data/history.csv 에 날짜별로 쌓고,
  docs/index.html(휴대폰으로 보는 표), docs/kakao.txt(카톡용 요약)를 만든다.
- 추가 설치 없이 파이썬 기본 기능만 사용.
"""
import csv
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config.json")
HISTORY = os.path.join(ROOT, "data", "history.csv")
DOCS = os.path.join(ROOT, "docs")
API = "https://openapi.naver.com/v1/search/shop.json"
FIELDS = ["date", "product", "keyword", "rank", "title", "link"]
KAKAO_LIMIT = 200
KAKAO_SEP = "-----"


# ---------- 네이버 API ----------
def naver_fetch(query, start, display):
    cid = os.environ.get("NAVER_CLIENT_ID", "")
    sec = os.environ.get("NAVER_CLIENT_SECRET", "")
    if not cid or not sec:
        sys.exit("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 이 설정되지 않았습니다.")
    qs = urllib.parse.urlencode(
        {"query": query, "display": display, "start": start, "sort": "sim"})
    req = urllib.request.Request(API + "?" + qs, headers={
        "X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            body = e.read().decode("utf-8", "ignore")[:300]
            raise RuntimeError(f"네이버 API 오류 {e.code}: {body}")
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise


def clean(text):
    return re.sub(r"<[^>]+>", "", html.unescape(text or "")).strip()


def norm(text):
    return re.sub(r"\s+", "", text or "").lower()


def matches(item, product):
    if norm(item.get("mallName")) != norm(product["mall_name"]):
        return False
    ids = [str(x) for x in product.get("product_ids") or []]
    if ids and str(item.get("productId")) not in ids:
        return False
    title = norm(clean(item.get("title")))
    return all(norm(w) in title for w in product.get("title_contains") or [])


def find_rank(keyword, product, max_rank, fetch=naver_fetch):
    """(순위, 상품) 반환. 못 찾으면 (None, None)."""
    max_rank = min(max_rank, 1000)  # API 는 1000위까지만 조회 가능
    for start in range(1, max_rank + 1, 100):
        display = min(100, max_rank - start + 1)
        items = fetch(keyword, start, display).get("items", [])
        for i, item in enumerate(items):
            if matches(item, product):
                return start + i, item
        if len(items) < display:
            break
        time.sleep(0.12)
    return None, None


# ---------- 기록 ----------
def load_history():
    if not os.path.exists(HISTORY):
        return []
    with open(HISTORY, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def save_history(rows):
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    rows.sort(key=lambda r: (r["date"], r["product"]))
    with open(HISTORY, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def rank_map(rows):
    """{(상품, 키워드): {날짜: 순위 or None}}"""
    m = {}
    for r in rows:
        m.setdefault((r["product"], r["keyword"]), {})[r["date"]] = (
            int(r["rank"]) if r["rank"] else None)
    return m


def change_text(today, before, tracked_before=True):
    """셀하처럼 ▲(오름) ▼(내림) 표시. 숫자가 작아지면 순위가 오른 것."""
    if today is None and before is None:
        return ""
    if before is None:
        return "(진입)" if tracked_before else "(신규)"
    if today is None:
        return "(밖으로)"
    d = before - today
    if d > 0:
        return f"(▲{d})"
    if d < 0:
        return f"(▼{-d})"
    return "(-)"


def rank_text(rank, max_rank):
    return f"{rank}위" if rank else f"{max_rank}위 밖"


# ---------- 카톡 요약 ----------
def kakao_messages(date, products, ranks, prev_date, max_rank):
    msgs = []
    md = f"{int(date[5:7])}/{int(date[8:10])}"
    for p in products:
        found, missing = [], []
        for kw in p["keywords"]:
            hist = ranks.get((p["name"], kw), {})
            r = hist.get(date)
            if r:
                ch = change_text(r, hist.get(prev_date), prev_date in hist) if prev_date else ""
                found.append((r, f"{kw} {r}위{ch}"))
            else:
                prev = hist.get(prev_date) if prev_date else None
                missing.append(kw + ("(밖으로)" if prev else ""))
        found.sort()
        lines = [t for _, t in found]
        if missing:
            lines.append(f"{max_rank}위 밖 {len(missing)}개")
            dropped = [m for m in missing if m.endswith("(밖으로)")]
            if dropped:
                lines.append("방금 밀려남: " + ", ".join(d[:-5] for d in dropped))
        head = f"[순위] {md} {p['name']}"
        cur = head
        for line in lines:
            if len(cur) + 1 + len(line) > KAKAO_LIMIT:
                msgs.append(cur)
                cur = f"{head} (계속)"
            cur += "\n" + line
        msgs.append(cur)
    return msgs


# ---------- 결과 페이지 ----------
def build_page(date, products, ranks, dates, max_rank):
    shown = sorted(dates, reverse=True)[:30]
    css = """
body{font-family:-apple-system,'Malgun Gothic',sans-serif;margin:0;padding:16px;background:#f6f7f9;color:#222}
h1{font-size:18px;margin:0 0 4px}p.sub{color:#777;font-size:12px;margin:0 0 16px}
h2{font-size:15px;margin:20px 0 8px}
.wrap{overflow-x:auto;background:#fff;border-radius:8px;border:1px solid #e3e5e8}
table{border-collapse:collapse;font-size:13px;white-space:nowrap}
th,td{padding:8px 12px;border-bottom:1px solid #eee;text-align:left}
th{background:#fafbfc;font-weight:600;color:#555;position:sticky;top:0}
td.kw,th.kw{position:sticky;left:0;background:#fff;font-weight:600;z-index:1}
th.kw{background:#fafbfc;z-index:2}
.out{color:#bbb}.up{color:#2e9e5b}.down{color:#e0566b}.same{color:#999}
"""
    parts = [f"<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>키워드 순위</title><style>{css}</style></head><body>",
             f"<h1>네이버쇼핑 키워드 순위</h1><p class='sub'>최근 확인: {date} · "
             f"네이버 검색 API 정확도순 기준, {max_rank}위까지 확인</p>"]
    for p in products:
        parts.append(f"<h2>{html.escape(p['name'])}</h2><div class='wrap'><table><tr><th class='kw'>키워드</th>")
        for d in shown:
            parts.append(f"<th>{int(d[5:7])}.{int(d[8:10])}</th>")
        parts.append("</tr>")

        def sort_key(kw):
            r = ranks.get((p["name"], kw), {}).get(date)
            return (r is None, r or 0)
        for kw in sorted(p["keywords"], key=sort_key):
            hist = ranks.get((p["name"], kw), {})
            parts.append(f"<tr><td class='kw'>{html.escape(kw)}</td>")
            for i, d in enumerate(shown):
                r = hist.get(d)
                older = shown[i + 1] if i + 1 < len(shown) else None
                if d not in hist:
                    parts.append("<td class='out'>·</td>")
                    continue
                if r is None:
                    parts.append(f"<td class='out'>{max_rank}위 밖</td>")
                    continue
                ch = change_text(r, hist.get(older)) if older and older in hist else ""
                cls = "up" if ("▲" in ch or "진입" in ch) else "down" if "▼" in ch else "same"
                ch = ch.strip("()") if ch not in ("(-)", "") else ""
                parts.append(f"<td>{r}위 <span class='{cls}'>{ch}</span></td>")
            parts.append("</tr>")
        parts.append("</table></div>")
    parts.append("</body></html>")
    return "".join(parts)


# ---------- 실행 ----------
def run(fetch=naver_fetch, today=None):
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    max_rank = int(cfg.get("max_rank", 300))
    products = cfg["products"]
    date = today or datetime.now(KST).strftime("%Y-%m-%d")

    rows = [r for r in load_history() if r["date"] != date]  # 같은 날 다시 돌리면 덮어씀
    for p in products:
        for kw in p["keywords"]:
            rank, item = find_rank(kw, p, max_rank, fetch)
            rows.append({"date": date, "product": p["name"], "keyword": kw,
                         "rank": rank or "", "title": clean(item["title"]) if item else "",
                         "link": item.get("link", "") if item else ""})
            print(f"{p['name']} | {kw} | {rank_text(rank, max_rank)}")
    save_history(rows)

    ranks = rank_map(rows)
    dates = sorted({r["date"] for r in rows})
    prev_dates = [d for d in dates if d < date]
    prev_date = prev_dates[-1] if prev_dates else None

    os.makedirs(DOCS, exist_ok=True)
    msgs = kakao_messages(date, products, ranks, prev_date, max_rank)
    with open(os.path.join(DOCS, "kakao.txt"), "w", encoding="utf-8") as f:
        f.write(f"\n{KAKAO_SEP}\n".join(msgs) + "\n")
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_page(date, products, ranks, dates, max_rank))
    return msgs


if __name__ == "__main__":
    for m in run():
        print("\n" + m)
