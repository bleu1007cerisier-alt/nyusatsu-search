# -*- coding: utf-8 -*-
"""神奈川県 かながわ電子入札共同システム（入札情報公開＝DENTYO/GPPI）をPlaywrightで収集し
tenders.csv に投入する。

GPPI は Staveware 系の AJAX SPA（_csrf/tabId）で raw HTTP では検索できない。公開導線は
  GPPI_MENU → 団体ボタン[data-code=0001]（神奈川県）→ ポータル(P5000_10/Information)
  → 業種メニューの「入札公告」 P5510(工事)/P6010(コンサル)/P6510(物品・一般委託)
  → 検索フォーム(掲載年度/表示件数)で検索 → 結果表(HTML)を解析（番号ページャ a.pagenation）。
結果表に調達案件番号・部局・入札方式・工種・開札予定日・件名・場所・期限が揃うのでそれを取り込む。

使い方（ローカルで Playwright 導入済みの環境）:
    python scripts/scrape_kanagawa_playwright.py            # ドライラン
    python scripts/scrape_kanagawa_playwright.py --write    # tenders.csv に反映（冪等）

CIには Playwright を載せていないため手動リフレッシュ用。到達先は新ホスト(AWS)で旧GP5000とは別。
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
MENU = "https://ebid-joho.e-kanagawa.lg.jp/DENTYO/GPPI_MENU"
BASE_URL = "https://ebid-joho.e-kanagawa.lg.jp/DENTYO/GPPI_MENU"
DANTAI = "0001"          # 神奈川県
NENDO_RE = r"令和\s*0?8"  # 現年度=令和8。年度替わりで更新すること。
CODES = {"P5510": "工事", "P6010": "コンサル", "P6510": "物品・一般委託"}


def _riso(s):
    m = re.search(r"令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", s or "")
    return f"{2018 + int(m.group(1))}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""


def _fetch():
    from playwright.sync_api import sync_playwright
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(ignore_https_errors=True)
        pg = ctx.new_page()
        pg.on("dialog", lambda d: d.accept())

        def parse_page():
            return pg.evaluate(r"""()=>{
              const tbs=[...document.querySelectorAll('table')];
              const tb=tbs.find(t=>/調達案件番号/.test(t.innerText)); if(!tb)return{hdr:[],rows:[]};
              const trs=[...tb.querySelectorAll('tr')];
              const cells=tr=>[...tr.querySelectorAll('td,th')].map(c=>c.innerText.trim().replace(/\s+/g,' '));
              const hi=trs.findIndex(r=>/調達案件番号/.test(r.innerText)); const hdr=cells(trs[hi]);
              const rows=trs.slice(hi+1).map(cells).filter(r=>/\d{15,}/.test(r.join('')));
              return {hdr,rows};
            }""")

        for code, nm in CODES.items():
            pg.goto(MENU, wait_until="networkidle", timeout=60000)
            pg.wait_for_timeout(1600)
            pg.click(f'button[data-code="{DANTAI}"]')
            pg.wait_for_timeout(3000)
            pg.evaluate(f"""()=>{{const a=document.querySelector('#{code} a'); if(a)a.click();}}""")
            pg.wait_for_timeout(4500)
            pg.evaluate(r"""(nen)=>{const re=new RegExp(nen);
              const s=document.querySelector('select[name=keisaiNen]');
              if(s){const o=[...s.options].find(o=>re.test(o.text));if(o){s.value=o.value;s.dispatchEvent(new Event('change',{bubbles:true}));}}
              const ps=document.querySelector('select[name=pageSize]');
              if(ps){const o2=[...ps.options].find(o=>o.text.trim()=='100');if(o2){ps.value=o2.value;ps.dispatchEvent(new Event('change',{bubbles:true}));}}}""", NENDO_RE)
            pg.wait_for_timeout(400)
            pg.evaluate(r"""()=>{const el=[...document.querySelectorAll('button,a,input')].filter(e=>e.offsetParent).find(e=>((e.innerText||e.value||'').trim())=='検索');if(el)el.click();}""")
            for _ in range(18):
                pg.wait_for_timeout(1500)
                if pg.evaluate("()=>document.querySelectorAll('tr').length") > 3 or \
                   pg.evaluate("()=>/該当|0件/.test(document.body.innerText)"):
                    break
            hdr = None
            rows = []
            maxpage = pg.evaluate(r"""()=>{const ns=[...document.querySelectorAll('a.pagenation,li a')].map(a=>parseInt((a.innerText||'').trim())).filter(n=>n>0);return ns.length?Math.max(...ns):1;}""")
            for page in range(1, min(maxpage, 12) + 1):
                if page > 1:
                    ok = pg.evaluate(r"""(p)=>{const a=[...document.querySelectorAll('a.pagenation,li a')].find(x=>(x.innerText||'').trim()==String(p));if(a){a.click();return true;}return false;}""", page)
                    if not ok:
                        break
                    pg.wait_for_timeout(3500)
                d = parse_page()
                if not hdr:
                    hdr = d["hdr"]
                rows += d["rows"]
            out[code] = {"name": nm, "hdr": hdr or [], "rows": rows}
            print("%s(%s): %d件" % (nm, code, len(rows)))
        b.close()
    return out


def _col(hdr, row, *names):
    for n in names:
        for i, h in enumerate(hdr):
            if n in h and i < len(row) and (row[i] or "").strip():
                return row[i].strip()
    return ""


def build_records(data, today):
    recs = []
    for code, blk in data.items():
        hdr = blk["hdr"]
        for row in blk["rows"]:
            name = _col(hdr, row, "工事名", "業務名")
            if not name or any(x in name for x in ("【中止】", "中止", "取止", "取りやめ")):
                continue
            opendt = _col(hdr, row, "開札 予定日", "開札予定日")
            od = _riso(opendt)
            if not od or datetime.date.fromisoformat(od) < today:  # 開札前のみ
                continue
            number = _col(hdr, row, "調達案件番号")
            bukyoku = _col(hdr, row, "入札執行部局名", "入札執行所属名")
            houshiki = _col(hdr, row, "入札 方式", "入札方式")
            gyoushu = _col(hdr, row, "工種", "業種", "営業種目")
            place = _col(hdr, row, "工事箇所", "履行場所")
            kigen = _col(hdr, row, "完成期限", "履行期限")
            title = re.sub(r"[　\s]+", " ", name).strip()
            cat = "プロポーザル" if re.search(r"プロポ|企画競争|企画提案", houshiki) else "入札"
            org = "神奈川県" + ((" " + bukyoku) if bukyoku else "")
            parts = [x for x in [
                ("場所: " + place) if place else "",
                ("入札方式: " + houshiki) if houshiki else "",
                ("工種/業種: " + gyoushu) if gyoushu else "",
                ("完成/履行期限: " + kigen) if kigen else "",
            ] if x]
            summary = " ／ ".join(parts)
            sched = [{"date": od, "label": "開札予定日", "raw": opendt}]
            key = number or ("h" + hashlib.md5((title + "|" + od + "|" + org).encode("utf-8")).hexdigest()[:12])
            tags = generate_tags(title, summary, summary)
            for pat, tag in _ORG:
                if tag not in tags and pat.search(org):
                    tags.append(tag)
            recs.append({
                "title": title, "category": cat, "organization": org, "prefecture": "神奈川県",
                "published_at": "", "deadline": od, "close_date": od, "result_date": "",
                "project_code": number, "awardee": "", "awardee_checked": "", "amount": "", "budget_checked": "",
                "url": BASE_URL + "?c=" + key, "result_url": "", "source_category": gyoushu,
                "summary": summary, "detail": summary, "schedule": json.dumps(sched, ensure_ascii=False),
                "attachments": "", "attachments_checked": "", "tags": ",".join(tags), "source": "KANAGAWA",
            })
    return recs


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
    with open(TENDERS, encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        allrows = list(rd)
        cols = rd.fieldnames
    idcol = cols[0]
    kept = [r for r in allrows if r.get("source") != "KANAGAWA"]
    prev_fs = {r["url"]: r.get("first_seen", "") for r in allrows if r.get("source") == "KANAGAWA"}
    maxid = max(int(r[idcol]) for r in kept)
    exurls = {r["url"] for r in kept}
    new = [r for r in recs if r["url"] not in exurls]
    print("既存KANAGAWA除去:%d 非KANAGAWA:%d 新規:%d" % (len(allrows) - len(kept), len(kept), len(new)))
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
    print("[WRITE] KANAGAWA %d件 (id %d..%d)" % (len(outrows), maxid + 1, i))


def main():
    write = "--write" in sys.argv
    data = _fetch()
    recs = build_records(data, datetime.date.today())
    print("開札前レコード:", len(recs))
    ingest(recs, write)


if __name__ == "__main__":
    main()
