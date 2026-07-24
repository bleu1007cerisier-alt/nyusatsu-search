# -*- coding: utf-8 -*-
"""奈良県 公募型プロポーザル／企画競争（ソフト系業務委託）を県公式サイトから収集する。

奈良の入札情報サービス(DENCHO/PPJ)は建設工事・コンサル・物品の「入札」しか無く、
PR・調査・IT・イベント等の業務委託の「公募型プロポーザル」は県CMS(pref.nara.jp)の
入札情報ページ 16808.htm に掲載される。これを raw HTTP で収集し category=プロポーザルで投入。
url は各記事の安定URL(pref.nara.jp/nXXX/pXXXXXX.html)。締切/公告日は記事本文からラベル付きで抽出。

使い方: python scripts/scrape_nara_propo.py [--write]
source=NARA のうち url に pref.nara.jp を含む行(=プロポ)のみ入替。入札(ebid-kouji)は保持。
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
BASE = "https://www.pref.nara.jp"
HUB = BASE + "/16808.htm"
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_OP = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_CTX))
_OP.addheaders = [("User-Agent", "Mozilla/5.0")]

# 公募中（開いている）と判断するタイトル語 / 結果・終了を示す語
_OPEN_KW = re.compile(r"実施について|募集|公告|参加者|参加表明|企画提案|プロポーザル")
_CLOSED_KW = re.compile(r"選定結果|審査結果|実施結果|結果の公表|結果について|中止|終了")


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
        m = re.search(lab + r"[^0-9令]{0,12}((?:令和\s*\d+|\d{4})\s*年\s*\d+\s*月\s*\d+\s*日)", text)
        if m:
            d = _iso_any(m.group(1))
            if d:
                return d
    return ""


def fetch():
    h = _get(HUB)
    out = []
    seen = set()
    for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{6,80})</a>', h):
        href, title = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
        if not _OPEN_KW.search(title) or _CLOSED_KW.search(title):
            continue
        if not re.search(r"プロポ|企画提案|企画競争|公募", title):
            continue
        url = href if href.startswith("http") else BASE + ("" if href.startswith("/") else "/") + href
        if not re.search(r"pref\.nara\.jp", url) or url in seen:
            continue
        seen.add(url)
        out.append({"title": title, "url": url})
    return out


def build(items, today):
    recs = []
    for it in items:
        title = it["title"]
        try:
            art = re.sub(r"<[^>]+>", " ", _get(it["url"]))
            art = re.sub(r"\s+", " ", art)
        except Exception:
            art = ""
        deadline = _labeled_date(art, ["企画提案書.{0,6}提出期限", "提出期限", "応募期限",
                                       "参加表明書.{0,6}提出", "参加申込", "申込期限", "受付期限", "締切"])
        pub = _labeled_date(art, ["公告日", "掲載日", "公示日", "公告"])
        org = "奈良県"
        parts = ["区分: 公募型プロポーザル"]
        if deadline:
            parts.append("提出期限: " + deadline)
        summary = " ／ ".join(parts)
        sched = []
        if pub:
            sched.append({"date": pub, "label": "公告日"})
        if deadline:
            sched.append({"date": deadline, "label": "提出期限"})
        tags = generate_tags(title, summary, summary)
        for pat, tag in _ORG:
            if tag not in tags and pat.search(org):
                tags.append(tag)
        recs.append({
            "title": title, "category": "プロポーザル", "organization": org, "prefecture": "奈良県",
            "published_at": pub, "deadline": deadline, "close_date": "", "result_date": "",
            "project_code": "", "awardee": "", "awardee_checked": "", "amount": "", "budget_checked": "",
            "url": it["url"], "result_url": "", "source_category": "",
            "summary": summary, "detail": summary, "schedule": json.dumps(sched, ensure_ascii=False),
            "attachments": "", "attachments_checked": "", "tags": ",".join(tags), "source": "NARA",
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
    # 安全ガード: 取得0件のとき既存プロポを削除しない（サイト障害での全消し防止）
    if not recs:
        print("[SKIP] 取得0件のため既存プロポを保持（削除・置換しない）")
        return
    with open(TENDERS, encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        allrows = list(rd)
        cols = rd.fieldnames
    idcol = cols[0]

    def is_propo(r):
        # 入札(url=ppi.ebid-kouji-gyoumu.pref.nara.jp)と区別。CMSプロポは ebid-kouji を含まない。
        u = r.get("url") or ""
        return r.get("source") == "NARA" and "pref.nara.jp" in u and "ebid-kouji" not in u
    kept = [r for r in allrows if not is_propo(r)]
    prev_fs = {r["url"]: r.get("first_seen", "") for r in allrows if is_propo(r)}
    maxid = max(int(r[idcol]) for r in kept)
    exurls = {r["url"] for r in kept}
    new = [r for r in recs if r["url"] not in exurls]
    print("既存プロポ除去:%d 保持:%d 新規プロポ:%d" % (len(allrows) - len(kept), len(kept), len(new)))
    if not write:
        for r in new[:8]:
            print("  ", r["deadline"] or "締切?", "|", r["title"][:44])
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
    print("[WRITE] 奈良プロポ %d件 (id %d..%d)" % (len(outrows), maxid + 1, i))


def main():
    write = "--write" in sys.argv
    recs = build(fetch(), datetime.date.today())
    print("公募(プロポ)レコード:", len(recs))
    ingest(recs, write)


if __name__ == "__main__":
    main()
