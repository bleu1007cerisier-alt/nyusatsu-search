# -*- coding: utf-8 -*-
"""奈良県 入札情報サービス（DENCHO/PPJ）をPlaywrightで収集し tenders.csv に投入する。

奈良県の電子入札情報公開システムは Staveware 製の AJAX SPA で、初期HTMLに案件データが
無く raw HTTP では取得できない。公開導線は
  PPJ0020_0010（トップ）→「案件情報」→「案件情報検索」→ PPJ0050_0010（検索フォーム）
で、検索後に「CSV出力」ボタンから全項目CSV（基本情報/公告情報/入札結果情報…）を
ダウンロードできる。工事タブとコンサル（業務委託）タブを別々に検索・出力する。

使い方（ローカルで Playwright 導入済みの環境）:
    python scripts/scrape_nara_playwright.py            # ドライラン
    python scripts/scrape_nara_playwright.py --write    # tenders.csv に反映（冪等）

CIには Playwright を載せていないため、本スクリプトは手動リフレッシュ用。
"""
import sys, io, os, re, csv, json, hashlib, datetime, tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, errors="replace", encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from scraper import generate_tags  # noqa: E402
try:
    from tag_master import ORG_TAG_RULES  # noqa: E402
except Exception:
    ORG_TAG_RULES = []
_ORG = [(re.compile(p), t) for p, t in ORG_TAG_RULES]

TENDERS = os.path.join(ROOT, "dataset", "tenders.csv")
TOP = "https://ppi.ebid-kouji-gyoumu.pref.nara.jp/DENCHO/PPJ/PPJ0020_0010/"
BASE_URL = TOP  # 詳細はSPAでURL化できないため、案件キー付きのトップURLを合成キーに使う
NENDO_LABEL = "令和8"  # 現年度。年度替わりで更新すること。


def _fetch_tab(pg, consul):
    """検索フォームに到達し、（コンサルなら切替）年度を選び検索→CSVダウンロードのパスを返す。"""
    pg.goto(TOP, wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(2500)
    # メニュー「案件情報」が描画されるまで明示的に待つ（GitHub IPからは描画が遅く、
    # 即clickだと30秒でLocator.clickタイムアウト→0件になっていた）。
    anken = pg.get_by_text("案件情報", exact=True).first
    anken.wait_for(state="visible", timeout=45000)
    anken.click()
    pg.wait_for_timeout(1200)
    pg.get_by_text("案件情報検索").first.click()
    pg.wait_for_selector("select", timeout=15000)
    pg.wait_for_timeout(2500)
    if consul:
        pg.get_by_text("コンサル", exact=True).first.click()
        pg.wait_for_timeout(2500)
    for s in pg.query_selector_all("select"):
        if s.is_visible() and NENDO_LABEL in s.inner_text():
            s.select_option(label=NENDO_LABEL)
            break
    pg.wait_for_timeout(2000)
    loc = pg.locator("button:has-text('検索')")
    for i in range(loc.count()):
        if loc.nth(i).is_visible():
            loc.nth(i).click()
            break
    for _ in range(24):  # 検索結果は発火後 最大~36秒で同一ページに描画される
        pg.wait_for_timeout(1500)
        if pg.eval_on_selector_all("tr", "e=>e.length") > 1:
            break
    out = os.path.join(tempfile.gettempdir(), "nara_%s.csv" % ("consul" if consul else "koji"))
    with pg.expect_download(timeout=30000) as di:
        btn = pg.locator("button:has-text('CSV出力')")
        for i in range(btn.count()):
            if btn.nth(i).is_visible():
                btn.nth(i).click()
                break
    di.value.save_as(out)
    return out


def fetch_csvs():
    from playwright.sync_api import sync_playwright
    paths = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        # go.jp系のDENCHOはヘッドレス既定UAを嫌う可能性があるため実ブラウザ相当UA＋日本語ロケール。
        ctx = b.new_context(
            ignore_https_errors=True, accept_downloads=True, locale="ja-JP",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"))
        pg = ctx.new_page()
        pg.on("dialog", lambda d: d.accept())
        for consul in (False, True):
            try:
                paths.append(_fetch_tab(pg, consul))
                print("取得:", "コンサル" if consul else "工事", "->", paths[-1])
            except Exception as e:
                print("取得失敗", "コンサル" if consul else "工事", str(e)[:120])
            pg.wait_for_timeout(1000)
        b.close()
    return paths


def _pdate(s):
    s = (s or "").strip()
    try:
        return datetime.datetime.strptime(s[:10], "%Y/%m/%d").date()
    except Exception:
        return None


def _iso(s):
    d = _pdate(s)
    return d.strftime("%Y-%m-%d") if d else ""


def _col(d, *names):
    for n in names:
        if n in d and (d[n] or "").strip():
            return d[n].strip()
    return ""


def _yen(s):
    s = (s or "").strip().replace(",", "")
    return f"{int(s):,}円（税抜）" if s.isdigit() else ""


def _sect(rows, name):
    hdr = next((r for r in rows if r and r[0] == name), None)
    if not hdr:
        return []
    return [dict(zip(hdr, r)) for r in rows if r and r[0] == name and r != hdr]


def _mkrec(title, org, pub, close, number, chotatsu, gyoushu, summary, sched,
           result_date="", awardee="", amount="", budget=""):
    key = number or chotatsu or ("h" + hashlib.md5(
        (title + "|" + (close or result_date) + "|" + org).encode("utf-8")).hexdigest()[:12])
    tags = generate_tags(title, summary, summary)
    for pat, tag in _ORG:
        if tag not in tags and pat.search(org):
            tags.append(tag)
    return {
        "title": title, "category": "入札", "organization": org, "prefecture": "奈良県",
        # 開札前は開札予定日を deadline に入れて「締切:未定」を回避。落札結果は closed のため deadline 空。
        "published_at": pub, "deadline": (close if not result_date else ""),
        "close_date": close, "result_date": result_date,
        "project_code": number, "awardee": awardee, "awardee_checked": ("1" if awardee else ""),
        "amount": amount, "budget_checked": ("1" if budget else ""),
        "url": BASE_URL + "?c=" + key, "result_url": "", "source_category": gyoushu,
        "summary": summary, "detail": summary, "schedule": json.dumps(sched, ensure_ascii=False),
        "attachments": "", "attachments_checked": "", "tags": ",".join(tags), "source": "NARA",
    }


def parse_records(path, today):
    """基本情報から開札前(公告)を、入札結果情報から落札結果を生成する。"""
    rows = list(csv.reader(io.StringIO(open(path, "rb").read().decode("cp932"))))
    kihon = _sect(rows, "基本情報")
    bmap = {}
    for d in kihon:
        num = _col(d, "工事番号", "業務番号")
        if num:
            bmap[num] = d
    out = []
    # 開札前(公告)
    for d in kihon:
        name = _col(d, "工事名", "業務名")
        if not name or any(x in name for x in ("【中止】", "中止", "取止", "取りやめ", "取り止め")):
            continue
        opendt = _col(d, "開札予定日時")
        od = _pdate(opendt)
        if not od or od < today:  # 開札前のみ
            continue
        number = _col(d, "工事番号", "業務番号")
        gyoushu = _col(d, "工種名", "業種名")
        org = "奈良県" + ((" " + _col(d, "部局名")) if _col(d, "部局名") else "") + \
              ((" " + _col(d, "所属名")) if _col(d, "所属名") else "")
        pub = _iso(_col(d, "公告日時／指名通知日時"))
        close = _iso(opendt)
        title = re.sub(r"[　\s]+", " ", name).strip()
        parts = [x for x in [
            ("場所: " + _col(d, "工事場所", "履行場所")) if _col(d, "工事場所", "履行場所") else "",
            ("入札方式: " + _col(d, "入札方式名")) if _col(d, "入札方式名") else "",
            ("工種/業種: " + gyoushu) if gyoushu else "",
            ("工期/履行期間: " + _col(d, "工期", "履行期間")) if _col(d, "工期", "履行期間") else "",
        ] if x]
        sched = []
        if pub:
            sched.append({"date": pub, "label": "公告日"})
        if close:
            sched.append({"date": close, "label": "開札予定日", "raw": opendt})
        out.append(_mkrec(title, org, pub, close, number, _col(d, "調達案件番号"),
                          gyoushu, " ／ ".join(parts), sched))
    # 落札結果
    for d in _sect(rows, "入札結果情報"):
        awardee = _col(d, "落札者")
        if not awardee:
            continue
        number = _col(d, "工事番号", "業務番号")
        b = bmap.get(number, {})
        gyoushu = _col(b, "工種名", "業種名")
        org = "奈良県" + ((" " + _col(d, "部局名")) if _col(d, "部局名") else "") + \
              ((" " + _col(d, "所属名")) if _col(d, "所属名") else "")
        pub = _iso(_col(b, "公告日時／指名通知日時"))
        rdate = _iso(_col(b, "開札予定日時")) or _iso(_col(d, "公開期間開始日"))
        title = re.sub(r"[　\s]+", " ", _col(d, "工事名", "業務名")).strip()
        amount = _yen(_col(d, "落札金額（税抜）"))
        yotei = _yen(_col(d, "予定価格（税抜き）", "予定価格（税抜）"))
        parts = [x for x in [
            "落札者: " + awardee,
            ("落札金額: " + amount) if amount else "",
            ("予定価格: " + yotei) if yotei else "",
            ("入札方式: " + _col(b, "入札方式名")) if _col(b, "入札方式名") else "",
            ("工種/業種: " + gyoushu) if gyoushu else "",
            ("場所: " + _col(b, "工事場所", "履行場所")) if _col(b, "工事場所", "履行場所") else "",
        ] if x]
        sched = []
        if pub:
            sched.append({"date": pub, "label": "公告日"})
        if rdate:
            sched.append({"date": rdate, "label": "開札日"})
        out.append(_mkrec(title, org, pub, "", number, _col(d, "調達案件番号"),
                          gyoushu, " ／ ".join(parts), sched,
                          result_date=rdate, awardee=awardee, amount=amount, budget=yotei))
    return out


def ingest(recs, write):
    today_s = datetime.date.today().isoformat()
    now_s = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    # バッチ内URL重複除去
    seen, dedup = set(), []
    for r in recs:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        dedup.append(r)
    recs = dedup
    # 安全ガード: スクレイプ0件のとき既存行を削除しない（サイト障害・SPAロード失敗で
    # 全件消失する事故の防止。2026-07-24に奈良で入札+落札結果784件消失した実績あり）。
    if not recs:
        print("[SKIP] スクレイプ結果0件のため既存データを保持（削除・置換しない）")
        return
    with open(TENDERS, encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        allrows = list(rd)
        cols = rd.fieldnames
    idcol = cols[0]
    # 入札情報サービス(DENCHO/PPJ, url=ebid-kouji)分のみ入替。県CMSのプロポ(pref.nara.jp)は保持。
    def is_nyusatsu(r):
        return r.get("source") == "NARA" and "ebid-kouji" in (r.get("url") or "")
    kept = [r for r in allrows if not is_nyusatsu(r)]
    prev_fs = {r["url"]: r.get("first_seen", "") for r in allrows if is_nyusatsu(r)}
    maxid = max(int(r[idcol]) for r in kept)
    exurls = {r["url"] for r in kept}
    new = [r for r in recs if r["url"] not in exurls]
    print("既存NARA除去:%d 非NARA保持:%d 新規NARA:%d" % (len(allrows) - len(kept), len(kept), len(new)))
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
    print("[WRITE] NARA %d件 (id %d..%d)" % (len(outrows), maxid + 1, i))


def main():
    write = "--write" in sys.argv
    today = datetime.date.today()
    paths = [p for p in fetch_csvs() if p and os.path.exists(p)]
    recs = []
    for p in paths:
        recs += parse_records(p, today)
    n_res = sum(1 for r in recs if r.get("awardee"))
    print("レコード: %d件（公告 %d / 落札結果 %d）" % (len(recs), len(recs) - n_res, n_res))
    ingest(recs, write)


if __name__ == "__main__":
    main()
