# -*- coding: utf-8 -*-
"""宮城県 建設工事等 入札情報サービス（efftis OTea フレームセット）をPlaywrightで収集し
tenders.csv に投入する（source=MIYAGI の建設分。物品/役務は別スクレイパーがraw収集）。

efftis OTea (miyagi.efftis.jp/04000/PPI/Public/Server/) はフレームセット＋JS駆動で raw 不可。
公開導線：Server/ → 左メニュー onBidNoticeSearch('00'=工事/'01'=コンサル) → 検索フォーム
（/Server フレーム）→ 公告日From=令和8年04月に絞る → onSearch() → 500件超なら「はい」で表示
→ 結果表（条件コメント混入）を案件行シグネチャ(令和X年度…号＋R日付)で抽出 → onNext()でページ送り。
開札予定日>=今日（開札前）のみ投入。公告日/開札予定日/部局/入札方式/工種/案件番号を収録。

使い方: python scripts/scrape_miyagi_kensetsu_playwright.py [--write]
CIには Playwright 未搭載のため手動リフレッシュ用。物品(source=MIYAGI, url=/04900)は本スクリプトの
置換対象外（url に /04000 を含む建設分のみ入替）。
"""
import sys, io, os, re, csv, json, hashlib, datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, errors="replace", encoding="utf-8")
csv.field_size_limit(10 ** 7)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from scraper import generate_tags  # noqa: E402
try:
    from tag_master import ORG_TAG_RULES  # noqa: E402
except Exception:
    ORG_TAG_RULES = []
_ORG = [(re.compile(p), t) for p, t in ORG_TAG_RULES]

TENDERS = os.path.join(ROOT, "dataset", "tenders.csv")
TOP = "https://miyagi.efftis.jp/04000/PPI/Public/Server/"
BASE_URL = "https://miyagi.efftis.jp/04000/PPI/Public/Server/"
NOTICE_FROM = "202604"  # 公告日From=令和8年04月（現年度。年度替わりで更新）


def _server(pg):
    sv = None
    for f in pg.frames:
        if f.url.rstrip("/").endswith("/Server"):
            sv = f
    return sv


def _parse(d):
    d = re.sub(r"<!--.*?-->", "", d, flags=re.S)
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", d, re.I | re.S):
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", x)).strip()
                 for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.I | re.S)]
        cells = [x for x in cells if x]
        if not any(re.search(r"令和\s*\d+\s*年度", c) for c in cells):
            continue
        if len([c for c in cells if re.fullmatch(r"R?\d{2}/\d{2}/\d{2}", c)]) < 2:
            continue
        out.append(cells)
    return out


def _rec(cells):
    ban = next((c for c in cells if re.search(r"令和\s*\d+\s*年度", c)), "")
    dates = [c for c in cells if re.fullmatch(r"R?\d{2}/\d{2}/\d{2}", c)]

    def diso(r):
        m = re.match(r"R?(\d{2})/(\d{2})/(\d{2})", r)
        return f"{2018 + int(m.group(1))}-{m.group(2)}-{m.group(3)}" if m else ""
    name = cells[cells.index(ban) + 1] if ban in cells and cells.index(ban) + 1 < len(cells) else ""
    bukyoku = next((c for c in cells if ("／" in c or "部" in c or "課" in c or "局" in c)
                    and c != name and not re.search(r"令和|R?\d{2}/", c)), "")
    houshiki = next((c for c in cells if "入札" in c and len(c) < 20 and c != name), "")
    gyoushu = next((c for c in cells if re.search(
        r"一式|設備|工事|舗装|造園|塗装|防水|施設|土木|建築|電気|管|機械|通信|さく井|法面|消防|コンサル|測量|設計|調査|補償|地質", c)
        and c not in (name, bukyoku, houshiki) and not re.search(r"令和|R?\d{2}/", c) and len(c) < 24), "")
    return {"ban": ban, "name": name, "bukyoku": bukyoku, "houshiki": houshiki,
            "gyoushu": gyoushu, "koukoku": diso(dates[0]) if dates else "",
            "kaisatsu": diso(dates[-1]) if dates else ""}


def fetch():
    from playwright.sync_api import sync_playwright
    rows = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(ignore_https_errors=True)
        pg = ctx.new_page()
        pg.on("dialog", lambda d: d.accept())
        for gyo in ("00", "01"):
            pg.goto(TOP, wait_until="networkidle", timeout=60000)
            pg.wait_for_timeout(3000)
            for f in pg.frames:
                try:
                    if "onBidNoticeSearch" in f.content():
                        f.evaluate(f"onBidNoticeSearch('{gyo}')")
                        break
                except Exception:
                    pass
            pg.wait_for_timeout(4500)
            ff = _server(pg)
            ff.evaluate(r"""(pre)=>{const s=document.querySelector('select[name=bid_Notice_Date_From]');const o=[...s.options].find(o=>o.value.indexOf(pre)===0);if(o)s.value=o.value;}""", NOTICE_FROM)
            ff.evaluate("onSearch()")
            pg.wait_for_timeout(2800)
            sv = _server(pg)
            for lbl in ("はい", "表示", "ＯＫ", "OK"):
                if sv.evaluate(r"""(lab)=>{const el=[...document.querySelectorAll('a,input,img,button')].find(e=>((e.value||e.alt||e.innerText||'').trim())==lab);if(el){el.click();return true;}return false;}""", lbl):
                    pg.wait_for_timeout(3500)
                    break
            seen = set()
            for _ in range(20):
                sv = _server(pg)
                for c in _parse(sv.content()):
                    k = "|".join(c)
                    if k not in seen:
                        seen.add(k)
                        rows.append(c)
                nx = sv.evaluate(r"""()=>{const el=[...document.querySelectorAll('a,input,img,button')].find(e=>/onNext\(/.test(e.getAttribute('onclick')||''));if(el && !/disabled/.test(el.className||'')){el.click();return true;}return false;}""")
                if not nx:
                    break
                pg.wait_for_timeout(3000)
        b.close()
    return [_rec(c) for c in rows]


def build(records, today):
    out = []
    for d in records:
        name = (d.get("name") or "").strip()
        kaisatsu = d.get("kaisatsu") or ""
        if not name or not kaisatsu or any(x in name for x in ("【中止】", "中止", "取止", "取りやめ")):
            continue
        if datetime.date.fromisoformat(kaisatsu) < today:
            continue
        houshiki = d.get("houshiki") or ""
        gyoushu = d.get("gyoushu") or ""
        bukyoku = re.sub(r"[　\s]+", " ", d.get("bukyoku") or "").strip()
        ban = d.get("ban") or ""
        koukoku = d.get("koukoku") or ""
        title = re.sub(r"[　\s]+", " ", name).strip()
        cat = "プロポーザル" if re.search(r"プロポ|企画競争|企画提案", houshiki) else "入札"
        org = "宮城県" + ((" " + bukyoku) if bukyoku else "")
        parts = [x for x in [("入札方式: " + houshiki) if houshiki else "",
                             ("工種/業種: " + gyoushu) if gyoushu else "",
                             ("案件番号: " + ban) if ban else ""] if x]
        summary = " ／ ".join(parts)
        sched = []
        if koukoku:
            sched.append({"date": koukoku, "label": "公告日"})
        if kaisatsu:
            sched.append({"date": kaisatsu, "label": "開札予定日"})
        key = "h" + hashlib.md5((ban + "|" + title + "|" + kaisatsu).encode("utf-8")).hexdigest()[:12]
        tags = generate_tags(title, summary, summary)
        for pat, tag in _ORG:
            if tag not in tags and pat.search(org):
                tags.append(tag)
        out.append({
            "title": title, "category": cat, "organization": org, "prefecture": "宮城県",
            "published_at": koukoku, "deadline": kaisatsu, "close_date": kaisatsu, "result_date": "",
            "project_code": ban, "awardee": "", "awardee_checked": "", "amount": "", "budget_checked": "",
            "url": BASE_URL + "?c=" + key, "result_url": "", "source_category": gyoushu,
            "summary": summary, "detail": summary, "schedule": json.dumps(sched, ensure_ascii=False),
            "attachments": "", "attachments_checked": "", "tags": ",".join(tags), "source": "MIYAGI",
        })
    return out


def ingest(recs, write):
    today_s = datetime.date.today().isoformat()
    now_s = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    seen, dedup = set(), []
    for r in recs:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        dedup.append(r)
    recs = dedup
    # 安全ガード: スクレイプ0件のとき既存行を削除しない（サイト障害での全件消失を防止）
    if not recs:
        print("[SKIP] スクレイプ結果0件のため既存データを保持（削除・置換しない）")
        return
    with open(TENDERS, encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        allrows = list(rd)
        cols = rd.fieldnames
    idcol = cols[0]

    def is_kensetsu(r):
        return r.get("source") == "MIYAGI" and "04000" in (r.get("url") or "")
    kept = [r for r in allrows if not is_kensetsu(r)]  # 物品(04900)は保持
    prev_fs = {r["url"]: r.get("first_seen", "") for r in allrows if is_kensetsu(r)}
    maxid = max(int(r[idcol]) for r in kept)
    exurls = {r["url"] for r in kept}
    new = [r for r in recs if r["url"] not in exurls]
    print("既存建設除去:%d 保持:%d 新規建設:%d" % (len(allrows) - len(kept), len(kept), len(new)))
    if not write:
        print("[DRY RUN] --write で反映")
        return
    i = maxid
    outrows = []
    for r in new:
        i += 1
        row = {c: "" for c in cols}
        row[idcol] = str(i)
        for k, v in r.items():
            if k in row:
                row[k] = v
        row["first_seen"] = prev_fs.get(r["url"], today_s)
        row["last_seen"] = now_s
        outrows.append(row)
    with open(TENDERS, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in kept:
            w.writerow({c: r.get(c, "") for c in cols})
        for r in outrows:
            w.writerow(r)
    print("[WRITE] 宮城建設 %d件 (id %d..%d)" % (len(outrows), maxid + 1, i))


def main():
    write = "--write" in sys.argv
    recs = build(fetch(), datetime.date.today())
    print("開札前レコード:", len(recs))
    ingest(recs, write)


if __name__ == "__main__":
    main()
