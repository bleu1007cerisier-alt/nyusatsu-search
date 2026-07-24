# -*- coding: utf-8 -*-
"""電子入札システムのみ実装で「公募型プロポーザル/企画競争」を取りこぼしていた県について、
県公式サイトの公募/入札情報ページから業務委託の公募案件を収集する（config駆動・raw HTTP）。

各県 config: source / pref / hubs(集約ページURL) / domain(そのソースのプロポを識別するurl部分文字列)。
共通処理: ハブから<a>を抽出→プロポ/企画提案系のみ採用(結果・補助金・委員募集等は除外)→記事URLで一意化
→category=プロポーザルで投入。source内のプロポ(domain一致)のみ入替し、入札(別url)は保持。
締切/公告日は記事本文からラベル付きで best-effort 抽出（取れなくても公募自体は捕捉）。

使い方: python scripts/scrape_pref_propo.py [--write] [SOURCE...]   (SOURCE指定で対象限定)
"""
import sys, io, os, re, csv, json, datetime, ssl, urllib.request

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

# 県別config。domain=そのソースのプロポ行を識別/入替するためのurl部分文字列。
CONFIGS = {
    "AOMORI": {"pref": "青森県", "base": "https://www.pref.aomori.lg.jp",
               "hubs": ["https://www.pref.aomori.lg.jp/boshu/index_1.html"],
               "domain": "www.pref.aomori.lg.jp"},  # 入札はpub.pref.aomori（www.でなく）で区別
    "MIYAGI": {"pref": "宮城県", "base": "https://www.pref.miyagi.jp",
               "hubs": ["https://www.pref.miyagi.jp/soshiki/keiyaku/r7puropo.html"],
               "domain": "pref.miyagi.jp"},  # 入札はmiyagi.efftis.jpで区別
    "MIYAZAKI": {"pref": "宮崎県", "base": "https://www.pref.miyazaki.lg.jp",
                 "hubs": ["https://www.pref.miyazaki.lg.jp/kense/chotatsu/itaku/kikakutean/index.html"],
                 "domain": "www.pref.miyazaki.lg.jp", "recent_days": 60},  # 入札はwww.e-nyusatsu-joho.pref…で区別
    "IBARAKI": {"pref": "茨城県", "base": "https://www.pref.ibaraki.jp",
                # 全庁横断の新着情報(/news.html)＋募集ページ。プロポは各部局に散在するため
                # 全庁新着から拾うのが最も網羅的。入札はppi.cals-ibaraki.lg.jpで区別。
                "hubs": ["https://www.pref.ibaraki.jp/news.html",
                         "https://www.pref.ibaraki.jp/bosyu.html"],
                "domain": "www.pref.ibaraki.jp"},
    "KAGOSHIMA": {"pref": "鹿児島県", "base": "https://www.pref.kagoshima.jp",
                  "hubs": ["https://www.pref.kagoshima.jp/kensei/nyusatu/nyusatujoho/index.html"],
                  "domain": "www.pref.kagoshima.jp"},  # 入札はwww.kagoshima-nyusatsu.jpで区別
    "ISHIKAWA": {"pref": "石川県", "base": "https://www.pref.ishikawa.lg.jp",
                 # 中央一覧が無く各部局に散在するため全庁新着情報から拾う。
                 # 入札はwww.ep-bis.supercals.jp(SuperCALS)で区別。
                 "hubs": ["https://www.pref.ishikawa.lg.jp/shinchaku/index.html"],
                 "domain": "www.pref.ishikawa.lg.jp"},
}

# プロポ/企画競争として採用する語（いずれか含む）
_TAKE = re.compile(r"プロポーザル|企画提案|企画競争|企画競技|公募型")
# 実業務であることの担保（裸のナビ「プロポーザル」等を除外）
_WORK = re.compile(r"業務|委託|事業|運営|制作|支援|調査|作成|保守|点検|管理|開発|プロモーション|"
                   r"広報|設計|策定|研修|セミナー|イベント|構築|整備|募集|実施|選定")
# 除外（結果・終了・非調達の募集）
_DROP = re.compile(r"選定結果|審査結果|実施結果|結果の公表|結果について|中止|終了|"
                   r"補助金|交付金|助成|委員(候補|の募集|を募集|候補者)|欠員|推薦|"
                   r"要望調査|意見募集|パブリックコメント|職員採用|会計年度任用|指定管理者の指定")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_OP = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_CTX))
_OP.addheaders = [("User-Agent", "Mozilla/5.0")]


def _get(u):
    b = _OP.open(u, timeout=30).read()
    for e in ("utf-8", "cp932"):
        try:
            return b.decode(e)
        except Exception:
            pass
    return b.decode("utf-8", "replace")


def _iso_any(s):
    m = re.search(r"令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", s)
    if m:
        return f"{2018 + int(m.group(1))}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{4})\s*年\s*(\d+)\s*月\s*(\d+)\s*日", s)
    if m:
        return f"{int(m.group(1))}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def _labeled_date(text, labels):
    for lab in labels:
        m = re.search(lab + r"[^0-9令]{0,14}((?:令和\s*\d+|\d{4})\s*年\s*\d+\s*月\s*\d+\s*日)", text)
        if m:
            d = _iso_any(m.group(1))
            if d:
                return d
    return ""


def fetch(cfg):
    out, seen = [], set()
    for hub in cfg["hubs"]:
        try:
            h = _get(hub)
        except Exception as e:
            print("  hub取得失敗", hub, str(e)[:50])
            continue
        for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', h, re.S):
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()
            if len(title) < 8 or not _TAKE.search(title) or not _WORK.search(title) or _DROP.search(title):
                continue
            href = m.group(1)
            url = href if href.startswith("http") else cfg["base"] + ("" if href.startswith("/") else "/") + href
            if cfg["domain"] not in url or url in seen:
                continue
            seen.add(url)
            # ハブ内でリンク直前にある掲載日（宮崎等は行頭に日付）を拾う（タグ除去・広めに参照）
            pre = re.sub(r"<[^>]+>", " ", h[max(0, m.start() - 300):m.start()])
            hub_date = ""
            dm = list(re.finditer(r"(?:令和\s*\d+|\d{4})\s*年\s*\d+\s*月\s*\d+\s*日", pre))
            if dm:
                hub_date = _iso_any(dm[-1].group(0))
            out.append({"title": title, "url": url, "hub_date": hub_date})
    return out


def build(cfg, items):
    recs = []
    cutoff = ""
    if cfg.get("recent_days"):
        cutoff = (datetime.date.today() - datetime.timedelta(days=cfg["recent_days"])).isoformat()
    for it in items:
        # アーカイブ県は掲載日で最近分のみ採用（掲載日不明は陳腐化回避のため除外）
        if cutoff and (not it.get("hub_date") or it["hub_date"] < cutoff):
            continue
        try:
            art = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _get(it["url"])))
        except Exception:
            art = ""
        deadline = _labeled_date(art, ["企画提案書.{0,8}提出期限", "提出期限", "応募期限",
                                       "参加表明.{0,8}提出", "参加申込", "申込期限", "受付期限", "締切", "提出締切"])
        pub = it.get("hub_date") or _labeled_date(art, ["公告日", "掲載日", "公示日"])
        org = cfg["pref"]
        parts = ["区分: 公募型プロポーザル"]
        if deadline:
            parts.append("提出期限: " + deadline)
        summary = " ／ ".join(parts)
        sched = []
        if pub:
            sched.append({"date": pub, "label": "公告日"})
        if deadline:
            sched.append({"date": deadline, "label": "提出期限"})
        tags = generate_tags(it["title"], summary, summary)
        for pat, tag in _ORG:
            if tag not in tags and pat.search(org):
                tags.append(tag)
        recs.append({
            "title": it["title"], "category": "プロポーザル", "organization": org, "prefecture": cfg["pref"],
            "published_at": pub, "deadline": deadline, "close_date": "", "result_date": "",
            "project_code": "", "awardee": "", "awardee_checked": "", "amount": "", "budget_checked": "",
            "url": it["url"], "result_url": "", "source_category": "",
            "summary": summary, "detail": summary, "schedule": json.dumps(sched, ensure_ascii=False),
            "attachments": "", "attachments_checked": "", "tags": ",".join(tags), "source": cfg["_source"],
        })
    return recs


def ingest(all_recs, targets, write):
    today_s = datetime.date.today().isoformat()
    now_s = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    # url重複除去
    seen, recs = set(), []
    for r in all_recs:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        recs.append(r)
    with open(TENDERS, encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        allrows = list(rd)
        cols = rd.fieldnames
    idcol = cols[0]
    import collections
    # 追記型: 既存行は一切削除せず、まだ無いURLのプロポだけ追加する。
    # 新着情報ページはローリング（直近分のみ）なので、置換方式だと過去に公開された
    # プロポが毎回消えて蓄積しない。追記なら日次実行で新規公開分が積み上がる。
    # ユーザーの「データは消さない」方針とも一致し、サイト障害で0件でも既存は無傷
    # （＝空消し事故が原理的に起きない）。締切切れは締切日で「終了」表示されるだけ。
    exurls = {r.get("url") for r in allrows}
    maxid = max(int(r[idcol]) for r in allrows if (r.get(idcol) or "").isdigit())
    new = [r for r in recs if r["url"] not in exurls]
    print("追記型 既存保持:%d 新規プロポ:%d 県別:%s" % (
        len(allrows), len(new), dict(collections.Counter(r["source"] for r in new))))
    if not write:
        for r in new[:12]:
            print("   ", r["source"], r["deadline"] or "締切?", "|", r["title"][:40])
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
        row["first_seen"] = today_s
        row["last_seen"] = now_s
        outrows.append(row)
    with open(TENDERS, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in allrows:
            w.writerow({c: r.get(c, "") for c in cols})
        for r in outrows:
            w.writerow(r)
    print("[WRITE] プロポ新規 %d件 追加 (id %d..%d) / 総 %d件" % (
        len(outrows), maxid + 1, i, len(allrows) + len(outrows)))


def main():
    args = [a for a in sys.argv[1:] if a != "--write"]
    write = "--write" in sys.argv
    targets = [a for a in args if a in CONFIGS] or list(CONFIGS)
    all_recs = []
    for s in targets:
        cfg = dict(CONFIGS[s], _source=s)
        items = fetch(cfg)
        recs = build(cfg, items)
        print("%s(%s): 公募 %d件" % (cfg["pref"], s, len(recs)))
        all_recs += recs
    ingest(all_recs, targets, write)


if __name__ == "__main__":
    main()
