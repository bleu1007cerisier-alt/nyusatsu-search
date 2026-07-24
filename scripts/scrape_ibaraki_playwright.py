# -*- coding: utf-8 -*-
"""茨城県 建設工事等 入札情報公開システム（電子入札コアシステムPPI）をPlaywrightで収集し
tenders.csv に投入する。

茨城は会計/物品(ppi2.cals-ibaraki)と建設工事(ppi.cals-ibaraki)で別ホスト。建設工事側 ppi は
2004年版コアシステムPPI(フレームセット＋JS)で raw 不可。公開導線：
  KF000ShowAction → hachukikan=0000ZZZZZZ(茨城県)選択 → jsLink2(1工事/2コンサル)
  → KK000(frameset koukai_menu/koukai_main) → 発注情報参照(OrderInfoRefer画像)
  → KK301検索フォーム(KFK301FrameShow)。公告日は date_start/date_end 両方必須・YYYY/MM/DD形式。
  A300=100件、doSearch1()で検索、結果表を解析。開札予定日>=今日（開札前）のみ投入。
※物品(ppi2)は茨城県で該当0のため対象外。会計物品が復活したら別途。

使い方: python scripts/scrape_ibaraki_playwright.py [--write]
CIには Playwright 未搭載のため手動リフレッシュ用。公告日レンジは現年度(NENDO_START/END)を更新。
"""
import sys, io, os, re, csv, json, hashlib, datetime, html as _html

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
B = "http://ppi.cals-ibaraki.lg.jp/koukai/do/"
BASE_URL = B + "KK000ShowAction"
KIKAN = "0000ZZZZZZ"          # 茨城県
NENDO_START = "2026/04/01"    # 現年度（令和8年度）の公告日レンジ。年度替わりで更新。
NENDO_END = "2026/12/31"


def _cl(s):
    return re.sub(r"\s+", " ", _html.unescape(s or "").replace("\xa0", " ")).strip()


def _formframe(pg):
    for f in pg.frames:
        try:
            if "koujimei" in f.content():
                return f
        except Exception:
            pass
    return None


def _result_frame(pg):
    best = None
    for fr in pg.frames:
        try:
            d = fr.content()
        except Exception:
            continue
        if "KK301Search" in fr.url or "FrameShow" in fr.url or "検索結果一覧" in d:
            n = len(re.findall(r"\d{4}/\d{2}/\d{2}", d))
            if best is None or n > best[1]:
                best = (fr, n)
    return best[0] if best else None


def _parse(d):
    d = re.sub(r"<!--.*?-->", "", d, flags=re.S)
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", d, re.I | re.S):
        cells = [_cl(re.sub(r"<[^>]+>", "", x)) for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.I | re.S)]
        cells = [x for x in cells if x]
        if len(cells) >= 6 and sum(1 for x in cells if re.search(r"\d{4}/\d{2}/\d{2}", x)) >= 1 \
           and any(re.search(r"工事|業務|委託|整備|改修|工", x) for x in cells[:2]):
            out.append(cells)
    return out


def fetch():
    from playwright.sync_api import sync_playwright
    got = {}
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(ignore_https_errors=True)
        pg = ctx.new_page()
        pg.on("dialog", lambda d: d.accept())
        for gyo, nm in ((1, "工事"), (2, "コンサル")):
            pg.goto(B + "KF000ShowAction", wait_until="networkidle", timeout=45000)
            pg.wait_for_timeout(1300)
            pg.select_option("select[name=hachukikan]", KIKAN)
            pg.wait_for_timeout(400)
            pg.evaluate(f"jsLink2({gyo})")
            pg.wait_for_timeout(2800)
            for fr in pg.frames:
                try:
                    im = fr.query_selector("img[src*=OrderInfoRefer]")
                    if im:
                        (fr.query_selector("a:has(img[src*=OrderInfoRefer])") or im).click()
                        break
                except Exception:
                    pass
            pg.wait_for_timeout(3500)
            f = _formframe(pg)
            if not f:
                continue
            f.evaluate("""(a,z)=>{const s=document.querySelector('[name=date_start]');if(s)s.value=a;
              const e=document.querySelector('[name=date_end]');if(e)e.value=z;
              const p=document.querySelector('select[name=A300]');if(p){const o=[...p.options].find(o=>o.text.trim()=='100');if(o)p.value=o.value;}}""", NENDO_START, NENDO_END)
            f.evaluate("doSearch1()")
            pg.wait_for_timeout(4500)
            rows, seen = [], set()
            for _ in range(14):
                rf = _result_frame(pg) or f
                for c in _parse(rf.content()):
                    k = "|".join(c[:3])
                    if k not in seen:
                        seen.add(k)
                        rows.append(c)
                nx = False
                for fr in pg.frames:
                    try:
                        if fr.evaluate(r"""()=>{const a=[...document.querySelectorAll('a,img,input')].find(e=>/次|Next|進む/.test((e.innerText||e.alt||e.value||''))&&!/最後/.test(e.innerText||e.alt||''));if(a){a.click();return true;}return false;}"""):
                            nx = True
                            break
                    except Exception:
                        pass
                if not nx:
                    break
                pg.wait_for_timeout(3000)
            got[nm] = rows
        b.close()
    return got


def _diso(s):
    m = re.search(r"(\d{4})/(\d{2})/(\d{2})", s or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def _yen(s):
    s = re.sub(r"[,\s円]", "", (s or "").replace("\xa0", ""))
    return f"{int(s):,}円（予定価格）" if s.isdigit() else ""


def build(data, today):
    out = []
    for nm, rows in data.items():
        for c in rows:
            c = [_cl(x) for x in c]
            if len(c) < 8:
                continue
            name = c[0]
            if not name or any(x in name for x in ("【※取止め】", "取止", "中止", "取りやめ")):
                continue
            ban, houshiki, gyoushu, basho, gaiyo = c[1], c[2], c[3], c[4], c[5]
            koukoku, kaisatsu = _diso(c[6]), _diso(c[7])
            amt = _yen(c[8]) if len(c) > 8 else ""
            if not kaisatsu or datetime.date.fromisoformat(kaisatsu) < today:
                continue
            if "****" in gyoushu:
                gyoushu = ""
            title = re.sub(r"[　\s]+", " ", name).strip()
            cat = "プロポーザル" if re.search(r"プロポ|企画競争|企画提案", houshiki) else "入札"
            org = "茨城県"
            parts = [x for x in [
                ("場所: " + basho) if basho else "",
                ("入札方式: " + houshiki) if houshiki else "",
                ("工種/業種: " + gyoushu) if gyoushu else "",
                ("概要: " + gaiyo) if gaiyo and len(gaiyo) > 1 else "",
            ] if x]  # 予定価格は amount フィールド(💴表示)にあるので事業内容には重複記載しない
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
                "title": title, "category": cat, "organization": org, "prefecture": "茨城県",
                "published_at": koukoku, "deadline": kaisatsu, "close_date": kaisatsu, "result_date": "",
                "project_code": ban, "awardee": "", "awardee_checked": "", "amount": amt,
                "budget_checked": ("1" if amt else ""),
                "url": BASE_URL + "?c=" + key, "result_url": "", "source_category": gyoushu,
                "summary": summary, "detail": summary, "schedule": json.dumps(sched, ensure_ascii=False),
                "attachments": "", "attachments_checked": "", "tags": ",".join(tags), "source": "IBARAKI",
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
    kept = [r for r in allrows if r.get("source") != "IBARAKI"]
    prev_fs = {r["url"]: r.get("first_seen", "") for r in allrows if r.get("source") == "IBARAKI"}
    maxid = max(int(r[idcol]) for r in kept)
    exurls = {r["url"] for r in kept}
    new = [r for r in recs if r["url"] not in exurls]
    print("既存IBARAKI除去:%d 非IBARAKI:%d 新規:%d" % (len(allrows) - len(kept), len(kept), len(new)))
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
    print("[WRITE] 茨城 %d件 (id %d..%d)" % (len(outrows), maxid + 1, i))


def main():
    write = "--write" in sys.argv
    recs = build(fetch(), datetime.date.today())
    print("開札前レコード:", len(recs))
    ingest(recs, write)


if __name__ == "__main__":
    main()
