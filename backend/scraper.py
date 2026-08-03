"""
スクレイパー：官公庁・公的機関の入札・公募情報を収集する。

設計方針:
  - 文字コードは自動判定（日本語の官公庁サイトは Shift_JIS が多い）
  - 静的HTMLで一覧を公開している、確実に取得できるソースのみを対象とする
  - 一覧 → 各詳細ページを取得し、締切・概要・タグまで収集する
  - 取得に失敗した場合のみサンプルデータにフォールバックする

現在の対象:
  - NEDO（新エネルギー・産業技術総合開発機構）公募情報
  - JST（科学技術振興機構）公募情報
"""

import aiohttp
import asyncio
import re
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# bot名を名乗るUAは go.jp 系のWAF（調達ポータル・NEDO・JST等）が
# データセンターIP(GitHub Actions)からのアクセスを弾き、接続タイムアウトを起こす。
# 実ブラウザ相当のUAに統一する（scraping上も安全側）。個別サイトで
# さらに厳しい場合は PORTAL_HEADERS（Accept/Sec-Fetch付き）を使う。
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# 詳細ページを取得する最大件数（負荷・速度対策）
MAX_DETAIL_FETCH = 30


# ---------------------------------------------------------------------------
# 共通ユーティリティ
# ---------------------------------------------------------------------------
def _decode(raw: bytes, content_type: str = "") -> str:
    """バイト列を適切な文字コードでデコードする（Shift_JIS / UTF-8 / EUC-JP を自動判定）。"""
    ct = (content_type or "").lower()
    head = raw[:3000].decode("ascii", "ignore").lower()
    blob = ct + " " + head
    if "shift_jis" in blob or "shift-jis" in blob or "x-sjis" in blob:
        enc = "cp932"
    elif "euc-jp" in blob or "euc_jp" in blob:
        enc = "euc-jp"
    else:
        enc = "utf-8"
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


async def fetch_bytes(session: aiohttp.ClientSession, url: str, retries: int = 3,
                      headers: dict = None, timeout: int = 30):
    """URLを取得して (本文バイト列, Content-Type) を返す。失敗時はリトライ、最終的に (b"", "")。

    headers: 省略時は共通HEADERS。bot対策の厳しいサイト（調達ポータル等）には
             実ブラウザ相当のヘッダを渡す。timeout: 総タイムアウト秒。
    """
    hdrs = headers or HEADERS
    for attempt in range(retries):
        try:
            await asyncio.sleep(0.7)  # サーバー負荷対策
            async with session.get(url, headers=hdrs, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                resp.raise_for_status()
                raw = await resp.read()
                return raw, resp.headers.get("Content-Type", "")
        except Exception as e:
            if attempt < retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            logger.error(f"取得失敗 {url}: {type(e).__name__} {e}")
            return b"", ""


def _normalize_date(text: str) -> str:
    """文字列中の最後の日付を 'YYYY-MM-DD' に正規化する（期間表記は終了日＝締切を採用）。"""
    # セル内の改行・空白を除去してから判定（年と月日の間に空白が入る表記に対応）
    text = re.sub(r"\s+", "", text or "")
    matches = re.findall(r"(20\d\d)\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})", text)
    if not matches:
        return ""
    y, mo, d = matches[-1]
    try:
        return f"{int(y)}-{int(mo):02d}-{int(d):02d}"
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# タグ付け
# ---------------------------------------------------------------------------
# タグ名 -> そのタグを付与するキーワード群
# タグマスター（実務者向け細分タグ体系）は tag_master.py で管理する
import unicodedata
from tag_master import TAG_MASTER, EXCLUDE_PATTERNS, flatten_master

TAG_KEYWORDS, TAG_CATEGORY = flatten_master()

# 交絡語（組織名・定型文・別分野の専門用語）を照合前に除去する
_TAG_EXCLUDE_RE = re.compile("|".join(EXCLUDE_PATTERNS))


def _normalize_for_tags(s: str) -> str:
    """タグ照合用の正規化（全角→半角・大文字→小文字・交絡語の除去）。"""
    n = unicodedata.normalize("NFKC", s or "").casefold()
    return _TAG_EXCLUDE_RE.sub(" ", n)


def _compile_tag_matchers():
    """タグごとの照合関数を事前コンパイルする。

    - 英数字のみ・8文字以下のキーワード（ai, dx, gis 等）は
      前後が英数字でないことを要求（"maintain" の ai 等の誤マッチ防止）
    - 先頭 "!" のキーワードは strict（タイトル＋要約のみ照合）
      → 入札説明書等の定型文（"詳細はホームページ" 等）による誤タグを防ぐ
    - それ以外は部分一致
    """
    matchers = []
    for tag, kws in TAG_KEYWORDS.items():
        pats = []
        for kw in kws:
            # 照合範囲: 0=全文, 1=タイトル+要約("!"), 2=タイトルのみ("!!")
            scope = 2 if kw.startswith("!!") else (1 if kw.startswith("!") else 0)
            body = kw.lstrip("!")
            if body.startswith("re:"):
                # 正規表現キーワード（複合語パターン用）。例: システムの設計・開発
                pats.append((scope, re.compile(_normalize_for_tags(body[3:]))))
                continue
            k = _normalize_for_tags(body)
            if re.fullmatch(r"[a-z0-9&\-\.]+", k) and len(k) <= 8:
                pats.append((scope, re.compile(
                    r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])")))
            else:
                pats.append((scope, k))  # 文字列＝単純部分一致
        matchers.append((tag, pats))
    return matchers


_TAG_MATCHERS = _compile_tag_matchers()


# 1件あたりのタグ上限。「重点領域の例示列挙」等を含む長文（スタートアップ広域公募等）は
# 無関係な分野語まで多数ヒットしうるため、タイトル+要約に現れる語を優先して上限内に収める。
MAX_TAGS_PER_ITEM = 10


def generate_tags(*texts: str, extra: Optional[List[str]] = None) -> List[str]:
    """タイトル・要約・本文などから実務粒度のタグを自動付与する。

    texts[0]（タイトル）、texts[:2]（タイトル＋要約等）、全文の3階層で照合する。
    "!!" キーワード＝タイトルのみ / "!" ＝タイトル＋要約 / 無印＝全文。
    タグ数が上限を超える場合、タイトル+要約に現れるタグを優先して残す
    （本文中の「対象分野の例示列挙」等による無関係タグの希釈を防ぐ）。
    """
    title_only = _normalize_for_tags(texts[0] if texts else "")
    primary = _normalize_for_tags(" ".join(t for t in texts[:2] if t))
    blob = _normalize_for_tags(" ".join(t for t in texts if t))
    targets = (blob, primary, title_only)  # scope=0,1,2
    primary_tags: List[str] = []   # タイトル+要約でも見つかった語（=信頼度が高い）
    detail_only_tags: List[str] = []  # 全文（本文含む）でのみ見つかった語
    for tag, pats in _TAG_MATCHERS:
        for scope, p in pats:
            target = targets[scope]
            hit = p.search(target) if hasattr(p, "search") else (p in target)
            if hit:
                in_primary = (scope != 0) or bool(
                    p.search(primary) if hasattr(p, "search") else (p in primary))
                (primary_tags if in_primary else detail_only_tags).append(tag)
                break
    tags = primary_tags + detail_only_tags[:max(0, MAX_TAGS_PER_ITEM - len(primary_tags))]
    if extra:
        for t in extra:
            if t and t not in tags:
                tags.append(t)
    return tags


# ---------------------------------------------------------------------------
# NEDO
# ---------------------------------------------------------------------------
def _extract_deadline(text: str) -> str:
    """詳細ページ本文から締切（申込期限・公募期間の終了日）を抽出する。

    - 改行をまたいで日付を拾う
    - 「A～B」の期間表記は終了日（B）＝締切を採用
    - 締切として確実性の高いキーワードを優先順に探索
    """
    flat = re.sub(r"\s+", " ", text)
    # 優先度順：明確な締切表現 → 期間表現（終了日を採用）
    keys = [
        "申込期限", "応募期限", "受付期限", "提出期限", "申請期限",
        "応募締切", "受付締切", "締切", "締め切り",
        "公募期間", "受付期間", "応募期間",
    ]
    date_re = r"20\d\d年\d{1,2}月\d{1,2}日"
    for k in keys:
        for m in re.finditer(re.escape(k) + r"[：:\s　]*([^。]{0,60})", flat):
            dates = re.findall(date_re, m.group(1))
            if dates:
                return _normalize_date(dates[-1])  # 最後の日付（期間の終了日＝締切）
    return ""


_SKIP_PARA = re.compile(
    r"(実施者を.{0,8}募集|を募集します|を募集する予定|を募集いたします|募集致します|"
    r"説明会を開催|オンライン.{0,4}説明会|Ｊグランツ|Jグランツ|応募期限|受付期間|"
    r"持参、郵送|契約約款|公募要領をご参照|以下のとおりです。詳細は|電子申請|お問い合わせ|"
    r"公式Ｘ|公式X|＠nedo|@nedo|フォロー|随時配信|ＳＮＳ)"
)

# 連絡先・メール等（概要から完全に除外する）
_CONTACT = re.compile(
    r"(担当者|問い?合わせ先?|問合せ先?|Ｅ[-－]?mail|E[-－]?mail|e[-－]?mail|"
    r"メールアドレス|アドレスの|\[\*\]|＠|@[\w\.]|nedo\.go\.jp|ＴＥＬ|TEL|電話|内線|FAX|ＦＡＸ)"
)


# 「実際の業務内容」を表す段落（背景説明より優先して概要の先頭に置く）
# 同一段落内に「主語(本事業等)」と「動詞(実施・調査等)」の両方があれば業務内容とみなす
_SCOPE_SUBJ = re.compile(r"(本事業|本調査|本業務|本公募|本研究|本プロジェクト|本制度|本取組|本委託|本件|本テーマ)")
_SCOPE_VERB = re.compile(r"(を実施|を行|を募集|を対象|を目的|に取り組|を支援|を構築|を開発|を整備|を調査|を検討|を策定|を推進|を目指)")


def _is_scope(p: str) -> bool:
    return bool(_SCOPE_SUBJ.search(p) and _SCOPE_VERB.search(p))


def _extract_overview(soup: BeautifulSoup) -> str:
    """詳細ページから「業務内容（何をする案件か）」を中心に要約を抽出する。

    募集アナウンス・手続き案内・連絡先を除外し、実際の業務内容を述べた段落を
    先頭に、続けて背景説明を補足として並べる。
    """
    def collect(skip_boiler: bool) -> List[str]:
        out: List[str] = []
        for p in soup.find_all("p"):
            t = p.get_text(strip=True)
            if len(t) < 25 or t.startswith("※"):
                continue
            if _CONTACT.search(t):
                continue
            if skip_boiler and _SKIP_PARA.search(t):
                continue
            out.append(t)
            if len(out) >= 6:
                break
        return out

    paras = collect(skip_boiler=True) or collect(skip_boiler=False)
    # 業務内容を述べた段落を先頭に並べ替え（背景説明は後ろへ）
    scope = [p for p in paras if _is_scope(p)]
    rest = [p for p in paras if p not in scope]
    ordered = (scope + rest)[:3]
    return "\n\n".join(ordered)[:1000]


_SCHED_DATE = r"(20\d\d年\d{1,2}月\d{1,2}日(?:（[月火水木金土日]）)?(?:[^。\n、]{0,10}?(?:まで|正午|時\d{0,2}分?))?)"


def _extract_schedule(text: str):
    """説明会・各種期限などの予定を時系列で抽出する。[{label, date, raw}] を返す。"""
    f = re.sub(r"\s+", "", text)
    items = []
    keys = [
        ("説明会", "開催日時"),
        ("説明会の申込期限", "申込期限"),
        ("応募締切", "応募期限"),
        ("提出締切", "提出期限"),
        ("質問受付期限", "質問受付期限"),
        ("質問締切", "質問期限"),
    ]
    for label, kw in keys:
        m = re.search(re.escape(kw) + r"[：:]?" + _SCHED_DATE, f)
        if m:
            d = _normalize_date(m.group(1))
            if d:
                items.append({"label": label, "date": d, "raw": m.group(1)})
    m = re.search(r"事前相談[^。]{0,40}?" + _SCHED_DATE, f)
    if m and _normalize_date(m.group(1)):
        items.append({"label": "事前相談", "date": _normalize_date(m.group(1)), "raw": m.group(1)})

    seen = set()
    uniq = []
    for it in sorted(items, key=lambda x: x["date"]):
        k = (it["label"], it["date"])
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    return uniq


def _parse_yen(s: str):
    """日本語の金額表記をおおよその円に変換する。変換不能なら None。"""
    s = s.replace(",", "").replace("，", "").replace("、", "")
    units = {"億": 10**8, "千万": 10**7, "百万": 10**6, "万": 10**4, "円": 1}
    total = 0.0
    found = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(億|千万|百万|万|円)", s):
        total += float(num) * units[unit]
        found = True
    return total if found else None


def _format_amount(raw: str) -> str:
    """金額表記を「○○万円」に統一する（例：1億5千万円未満（税込）→ 15,000万円未満（税込））。"""
    if not raw:
        return raw
    yen = _parse_yen(raw)
    if not yen or yen <= 0:
        return raw  # 変換できなければ原文のまま
    man = int(round(yen / 10**4))
    # 付帯表現を保持
    cond = next((c for c in ["未満", "以内", "以下", "程度", "まで", "以上"] if c in raw), "")
    tax = ""
    mt = re.search(r"(税込|税抜)", raw)
    if mt:
        tax = "（" + mt.group(1) + "）"
    per = "1件あたり" if re.search(r"1\s*件", raw) else ""
    return f"{per}{man:,}万円{cond}{tax}"


def _extract_budget(text: str) -> str:
    """公募詳細ページから「予算規模」を抽出し、万円表記に統一して返す。"""
    flat = re.sub(r"\s+", "", text)
    # 「予算規模：」「【予算規模】」「予算規模は」等、キーワード直後の区切り（】］：等）を許容
    m = re.search(
        r"予算規模[^0-9０-９億万千百]{0,5}([0-9０-９,，\.億万千百円以内未満程度税込税抜（）\(\)約\-―~～／/件]{2,45})",
        flat,
    )
    if not m:
        return ""
    val = m.group(1).strip("／/-")
    if "円" not in val:
        return ""
    m2 = re.match(r".*?円(?:以内|未満|程度|以下|台|規模)?(?:（税込）|（税抜）|\(税込\)|\(税抜\))?", val)
    val = m2.group(0) if m2 else val
    return _format_amount(val)


# 会社・機関名の判定パターン（接頭辞型と接尾辞型）。長音ー・中黒・々等も許容。
_ORG_NAME = r"[一-龥々〇ぁ-んァ-ヴーｱ-ﾝ・＆&’'\-Ａ-Ｚａ-ｚ０-９A-Za-z0-9]{2,28}"
_ORG_RE = (
    r"(?:株式会社|有限会社|合同会社|国立大学法人|公立大学法人|国立研究開発法人|"
    r"一般社団法人|公益財団法人|一般財団法人|公益社団法人)" + _ORG_NAME
    + r"|" + _ORG_NAME + r"(?:株式会社|有限会社|大学|高等専門学校|研究所|機構|協同組合)"
)


def _extract_awardee(text: str) -> str:
    """結果（実施体制の決定）ページから決定事業者（実施予定先）を抽出する。

    「実施予定先」ラベルの直後が会社・機関名で始まる箇所のみ採用する。
    会社名が添付資料にしか無いページでは空文字を返す（HTMLに無いものは取得しない）。
    """
    flat = re.sub(r"\s+", "", text)
    for m in re.finditer(
        r"(?:実施予定先|委託予定先|委託先|採択予定先|採択先|採択事業者|代表機関|落札者)[：:]?", flat
    ):
        seg = flat[m.end():m.end() + 160]
        # ラベル直後が会社・機関名で始まる箇所のみ採用（説明文や添付参照を除外）
        if not re.match(_ORG_RE, seg):
            continue
        # 次の節（番号付き見出し等）までを決定事業者の記載とみなす
        seg = re.split(
            r"\d[．.]|事業期間|募集要項|技術・事業分野|お問|（法人番号|採択審査|なお[、，]|住所",
            seg,
        )[0]
        seg = seg.strip("、，・。.（）()　 ")
        if "新エネルギー・産業技術総合開発機構" in seg:
            continue
        if 2 <= len(seg) <= 120:
            return seg
    return ""


NEDO_BASE = "https://www.nedo.go.jp"
# 取得対象の年度別一覧（当年度＋前年度を自動生成。年が変わっても自動対応）
def _nedo_year_lists() -> list:
    from datetime import date
    y = date.today().year
    return [f"/koubo/{y}_list.html", f"/koubo/{y - 1}_list.html"]
NEDO_YEAR_LISTS = _nedo_year_lists()

# 分野ページのテーブル列: [事業名, 予告掲載日, 公募開始日(リンク), 公募締切日, 結果(リンク)]
_DETAIL_HREF = re.compile(r"/koubo/[A-Za-z0-9_]+\.html")


def _abs(href: str) -> str:
    if not href:
        return ""
    return href if href.startswith("http") else NEDO_BASE + href


def _project_code(title: str) -> str:
    """タイトル先頭の 【P25011】 等から事業コードを取り出す。"""
    m = re.match(r"\s*【([^】]+)】", title)
    return m.group(1).strip() if m else ""


async def scrape_nedo() -> List[Dict]:
    """NEDO 公募情報を分野別ページの表から網羅的に取得する。

    年度別一覧 → 分野ページ → 表の各行を列ごとに解析し、
    公示日(公募開始日)・締切日(公募締切日)・結果日(結果)・事業コードを取得する。
    概要本文(detail)は別途 fetch_nedo_detail で取得する。
    """
    results: List[Dict] = []
    seen: set = set()

    async with aiohttp.ClientSession() as session:
        field_pages: Dict[str, str] = {}
        for ylist in NEDO_YEAR_LISTS:
            raw, ct = await fetch_bytes(session, NEDO_BASE + ylist,
                                        headers=PORTAL_HEADERS, timeout=60)
            if not raw:
                continue
            soup = BeautifulSoup(_decode(raw, ct), "html.parser")
            for a in soup.find_all("a", href=re.compile(r"/koubo/20\d\d_list_[0-9_]+\.html")):
                field_pages.setdefault(a["href"], a.get_text(strip=True))

        logger.info(f"NEDO: 分野ページ {len(field_pages)}件を巡回")

        for href, field_name in field_pages.items():
            raw, ct = await fetch_bytes(session, NEDO_BASE + href,
                                        headers=PORTAL_HEADERS, timeout=60)
            if not raw:
                continue
            soup = BeautifulSoup(_decode(raw, ct), "html.parser")
            table = soup.find("table")
            if not table:
                continue

            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 4:
                    continue  # ヘッダ行など

                title = tds[0].get_text(" ", strip=True)
                if not title or len(title) < 5:
                    continue

                yokoku = _normalize_date(tds[1].get_text())
                kaishi = _normalize_date(tds[2].get_text())
                shimekiri = _normalize_date(tds[3].get_text())
                kekka = _normalize_date(tds[4].get_text()) if len(tds) > 4 else ""

                # 公募詳細リンクは「公募開始日」列、結果リンクは「結果」列
                call_a = tds[2].find("a", href=_DETAIL_HREF)
                result_a = tds[4].find("a", href=_DETAIL_HREF) if len(tds) > 4 else None
                result_url = _abs(result_a["href"]) if result_a else ""
                url = _abs(call_a["href"]) if call_a else result_url

                # 行を一意に識別（同一事業の各回を区別）
                key = (title, kaishi, shimekiri, kekka)
                if key in seen:
                    continue
                seen.add(key)

                project_code = _project_code(title)
                title_clean = re.sub(r"^\s*【[^】]+】\s*", "", title).strip()
                tags = generate_tags(title_clean, field_name)
                results.append({
                    "title": title_clean,
                    "category": "プロポーザル",
                    "organization": "NEDO（新エネルギー・産業技術総合開発機構）",
                    "deadline": shimekiri,
                    "published_at": kaishi or yokoku,
                    "result_date": kekka,
                    "result_url": result_url,
                    "project_code": project_code,
                    "awardee": "",
                    "url": url,
                    "prefecture": "国",
                    "source": "NEDO",
                    "amount": "",
                    "source_category": field_name,
                    "summary": "",
                    "detail": "",
                    "tags": ",".join(tags),
                })

    logger.info(f"NEDO: {len(results)}件取得")
    return results


def _fetch_soup(url: str, retries: int = 3):
    """同期でページを取得（一時的な失敗に備えてリトライ）。"""
    import urllib.request
    import time as _time
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read()
                ct = resp.headers.get("Content-Type", "")
            return BeautifulSoup(_decode(raw, ct), "html.parser")
        except Exception as e:
            if attempt < retries - 1:
                _time.sleep(1.5 * (attempt + 1))
                continue
            logger.error(f"取得失敗 {url}: {e}")
            return None


def fetch_nedo_detail(url: str) -> Dict[str, str]:
    """NEDO公募詳細ページを同期取得し、概要と予算規模を返す。

    予算は本文の「予算規模：」を優先。本文に無ければ同ページの公募要領PDFから補完する。
    """
    soup = _fetch_soup(url)
    if soup is None:
        return {}
    text = soup.get_text("\n", strip=True)
    budget = _extract_budget(text)
    if not budget:
        budget = _pdf_budget_from_soup(soup)
    return {
        "detail": _extract_overview(soup),
        "budget": budget,
        "schedule": _extract_schedule(text),
        "attachments": _extract_attachment_links(soup),
    }


# 蓄積対象の添付ファイル種別（ラベルに含まれる語 → 種別名）
_ATTACH_KINDS = [
    ("公募要領", "公募要領"), ("募集要項", "公募要領"), ("仕様書", "仕様書"),
    ("評価", "評価基準"), ("採点", "評価基準"), ("審査", "審査基準"),
    ("基本計画", "基本計画"), ("提案書", "提案様式"),
]


def _extract_attachment_links(soup):
    """公募ページから蓄積対象の添付PDF（公募要領・仕様書・評価基準等）のリンクを抽出する。"""
    out = []
    seen = set()
    for a in soup.find_all("a", href=re.compile(r"\.pdf", re.I)):
        label = a.get_text(" ", strip=True)
        kind = next((k for key, k in _ATTACH_KINDS if key in label), "")
        if not kind:
            continue
        href = _abs(a.get("href", ""))
        if not href or href in seen:
            continue
        seen.add(href)
        out.append({"name": label, "url": href, "kind": kind})
    return out


def fetch_nedo_result(url: str) -> Dict[str, str]:
    """NEDO結果（実施体制の決定）ページから決定事業者を返す。"""
    soup = _fetch_soup(url)
    if soup is None:
        return {}
    text = soup.get_text("\n", strip=True)
    return {"awardee": _extract_awardee(text)}


_PDF_MONEY = r"([0-9０-９][0-9０-９,，\.]*(?:億|千万|百万|万)?円)"
_PDF_SUFFIX = r"(以内|以下|程度|まで|台)?"


def _zen2han(s: str) -> str:
    """全角数字・記号を半角に正規化する。"""
    return "".join(
        chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in s
    )


def _extract_pdf_budget(text: str) -> str:
    """公募要領PDFの本文から予算を抽出する（1件あたり優先→上限額類→全体予算）。万円表記に統一。"""
    flat = re.sub(r"\s+", "", _zen2han(text))

    # 1. 1件/テーマあたりの予算（応募者にとって最も重要）
    for kw in ["1件当たり", "1件あたり", "一件当たり",
               "1テーマ当たり", "1テーマあたり", "1事業者当たり", "1社当たり"]:
        m = re.search(re.escape(kw) + r"[^0-9億万千百]{0,10}" + _PDF_MONEY + _PDF_SUFFIX, flat)
        if m:
            return "1件あたり" + _format_amount(m.group(1) + (m.group(2) or ""))

    # 2. 予算規模（【予算規模】2,000万円以内 等）
    for m in re.finditer(r"予算規模[^0-9億万千百]{0,5}" + _PDF_MONEY + _PDF_SUFFIX, flat):
        if "提案内容次第" in flat[m.start():m.start() + 30]:
            continue
        return _format_amount(m.group(1) + (m.group(2) or ""))

    # 3. 上限額・委託費・契約上限額 など（幅広い表現に対応）
    upper_pats = [
        (r"上限額[：:]?" + _PDF_MONEY + _PDF_SUFFIX, ""),
        (r"上限金額[：:]?" + _PDF_MONEY + _PDF_SUFFIX, ""),
        (r"契約上限額[^0-9億万千百]{0,5}" + _PDF_MONEY + _PDF_SUFFIX, ""),
        (r"委託費[^0-9億万千百]{0,10}" + _PDF_MONEY + _PDF_SUFFIX, ""),
        (r"委託業務費[^0-9億万千百]{0,10}" + _PDF_MONEY + _PDF_SUFFIX, ""),
        (r"委託金額[^0-9億万千百]{0,5}" + _PDF_MONEY + _PDF_SUFFIX, ""),
        (r"交付上限額[^0-9億万千百]{0,5}" + _PDF_MONEY + _PDF_SUFFIX, ""),
        (r"補助上限額[^0-9億万千百]{0,5}" + _PDF_MONEY + _PDF_SUFFIX, ""),
        (r"費用の上限[^0-9億万千百]{0,5}" + _PDF_MONEY + _PDF_SUFFIX, ""),
        (r"補助金額[^0-9億万千百]{0,5}" + _PDF_MONEY + _PDF_SUFFIX, ""),
    ]
    for pat, prefix in upper_pats:
        m = re.search(pat, flat)
        if m:
            val = _format_amount(m.group(1) + (m.group(2) or ""))
            return (prefix + val) if prefix else val

    # 4. 全体予算・事業規模
    for kw in ["全体予算", "予算総額", "総事業費", "事業規模"]:
        for m in re.finditer(re.escape(kw) + r"[^0-9億万千百]{0,5}" + _PDF_MONEY + _PDF_SUFFIX, flat):
            if "取得" in flat[max(0, m.start() - 8):m.start()]:
                continue
            return "全体予算" + _format_amount(m.group(1) + (m.group(2) or ""))

    return ""


def _pdf_budget_from_soup(soup, page_url: str = "") -> str:
    """公募ページ内の公募要領PDFを探して予算を抽出する（本文に予算が無い場合のフォールバック）。"""
    import io
    import urllib.request
    from urllib.parse import urljoin

    def _resolve(href: str) -> str:
        if not href:
            return ""
        if href.startswith("http"):
            return href
        if page_url:
            return urljoin(page_url, href)
        return _abs(href)

    pdf_url = ""
    for a in soup.find_all("a", href=re.compile(r"\.pdf", re.I)):
        label = a.get_text(strip=True)
        href = a.get("href", "")
        if any(k in label for k in ("公募要領", "募集要項", "仕様書")):
            pdf_url = _resolve(href)
            break
        if not pdf_url:
            pdf_url = _resolve(href)
    if not pdf_url:
        return ""
    try:
        from pypdf import PdfReader
        req = urllib.request.Request(pdf_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = resp.read()
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:
        logger.error(f"PDF予算取得失敗 {pdf_url}: {e}")
        return ""
    return _extract_pdf_budget(text)


# ---------------------------------------------------------------------------
# JST（科学技術振興機構）
# ---------------------------------------------------------------------------
JST_BASE = "https://www.jst.go.jp"
JST_BOSYU = "/bosyu/bosyu.html"


def _jst_abs(href: str, page_url: str = "") -> str:
    """JST サイト内の相対パスを絶対 URL に変換する。"""
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return JST_BASE + href
    # ページ URL 基準の相対パス（../inter/... 等）
    from urllib.parse import urljoin
    base = page_url or (JST_BASE + JST_BOSYU)
    return urljoin(base, href)


async def scrape_jst() -> List[Dict]:
    """JST 公募情報を一覧ページから取得する。

    一覧テーブル: 締切日 | 分野 | タイトル（リンク、掲載日付き）
    外部ドメインへのリンク行はスキップ（JST が取りまとめている外部機関公募は別途検討）。
    """
    results: List[Dict] = []
    seen: set = set()

    async with aiohttp.ClientSession() as session:
        raw, ct = await fetch_bytes(session, JST_BASE + JST_BOSYU,
                                    headers=PORTAL_HEADERS, timeout=60)
        if not raw:
            logger.warning("JST: 一覧ページ取得失敗")
            return results
        soup = BeautifulSoup(_decode(raw, ct), "html.parser")

        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 3:
                    continue

                shimekiri = _normalize_date(tds[0].get_text())
                field = tds[1].get_text(strip=True)

                # タイトルとリンクを取得
                a = tds[2].find("a")
                if not a:
                    continue
                title = a.get_text(strip=True)
                href = a.get("href", "")
                if not href:
                    continue

                url = _jst_abs(href)

                # 外部ドメイン（JST 以外）はスキップ
                if not ("jst.go.jp" in url or url.startswith("/")):
                    continue

                # 掲載日を括弧内テキストから抽出（例: 「（2026年06月10日掲載）」）
                td_text = tds[2].get_text()
                pub_match = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日)", td_text)
                published_at = _normalize_date(pub_match.group(1)) if pub_match else ""

                if url in seen:
                    continue
                seen.add(url)

                tags = generate_tags(title, field)
                results.append({
                    "title": title,
                    "category": "プロポーザル",
                    "organization": "JST（科学技術振興機構）",
                    "deadline": shimekiri,
                    "published_at": published_at,
                    "result_date": "",
                    "result_url": "",
                    "project_code": "",
                    "awardee": "",
                    "url": url,
                    "prefecture": "国",
                    "source": "JST",
                    "amount": "",
                    "source_category": field,
                    "summary": "",
                    "detail": "",
                    "tags": ",".join(tags),
                })

    logger.info(f"JST: {len(results)}件取得")
    return results


def _jst_extract_attachments(soup, page_url: str):
    """JST 詳細ページから添付 PDF（公募要領・仕様書等）のリンクを抽出する。"""
    out = []
    seen = set()
    for a in soup.find_all("a", href=re.compile(r"\.pdf", re.I)):
        label = a.get_text(" ", strip=True)
        kind = next((k for key, k in _ATTACH_KINDS if key in label), "")
        if not kind:
            continue
        href = _jst_abs(a.get("href", ""), page_url)
        if not href or href in seen:
            continue
        seen.add(href)
        out.append({"name": label, "url": href, "kind": kind})
    return out


def fetch_jst_detail(url: str) -> Dict[str, str]:
    """JST 公募詳細ページを同期取得し、概要・予算・予定・添付を返す。

    NEDO と同じ汎用抽出関数を流用。本文に予算が無ければ同ページの PDF から補完する。
    採択結果リンクが見つかれば result_url も返す（公募終了後に追記される）。
    """
    soup = _fetch_soup(url)
    if soup is None:
        return {}
    text = soup.get_text("\n", strip=True)
    budget = _extract_budget(text)
    if not budget:
        budget = _pdf_budget_from_soup(soup, url)

    # 採択・選定結果ページへのリンクを探す
    _RESULT_KEYWORDS = ["採択課題", "採択結果", "選定結果", "採択者", "採択一覧", "実施予定先", "採択プロジェクト"]
    result_url = ""
    from urllib.parse import urljoin
    for a in soup.find_all("a", href=True):
        label = a.get_text(strip=True)
        if any(kw in label for kw in _RESULT_KEYWORDS):
            href = a["href"]
            result_url = href if href.startswith("http") else urljoin(url, href)
            break

    return {
        "detail": _extract_overview(soup),
        "budget": budget,
        "schedule": _extract_schedule(text),
        "attachments": _jst_extract_attachments(soup, url),
        "result_url": result_url,
    }


# ---------------------------------------------------------------------------
# 調達ポータル（デジタル庁 p-portal.go.jp）
# ---------------------------------------------------------------------------
PORTAL_BASE = "https://www.p-portal.go.jp"
PORTAL_FORM   = PORTAL_BASE + "/pps-web-biz/UAA01/OAA0101"
PORTAL_SEARCH = PORTAL_BASE + "/pps-web-biz/UAA01/OAA0100"
PORTAL_DETAIL = PORTAL_BASE + "/pps-web-biz/UAA01/OAA0104"

# 調達ポータルは bot 名の User-Agent + データセンターIP を WAF が弾き、
# GitHub Actions から接続がタイムアウトしていた（2026-07-28以降・新着が数日間ゼロ）。
# 実ブラウザ相当のヘッダを与えて突破する。他スクレイパーには影響させない。
PORTAL_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Connection": "keep-alive",
}

# 調達種別 → category/tags のマッピング
_PORTAL_TYPE_MAP = {
    "公募型プロポーザル": "プロポーザル",
    "企画競争": "プロポーザル",
    "随意契約": "随意契約",
    "一般競争入札": "入札",
    "指名競争入札": "入札",
    "オープンカウンタ": "入札",
    "資料提供招請": "RFI",
    "意見招請": "RFI",
    "入札公告": "入札",
    "落札": "",  # 落札公示は単独レコード化しない（既存行更新）
}


def _portal_category(choutatsushu: str) -> str:
    for kw, cat in _PORTAL_TYPE_MAP.items():
        if kw in choutatsushu:
            return cat
    return "入札"


def _reiwa_date(text: str) -> str:
    """元号付き日付を YYYY-MM-DD に変換する（令和/平成/昭和）。"""
    ERA = {"令和": 2018, "平成": 1988, "昭和": 1925}
    m = re.search(r"(令和|平成|昭和)(\d{1,2})年(\d{1,2})月(\d{1,2})日", text)
    if not m:
        # 西暦表記にもフォールバック
        return _normalize_date(text)
    base = ERA.get(m.group(1), 0)
    return f"{base + int(m.group(2))}-{int(m.group(3)):02d}-{int(m.group(4)):02d}"


def _portal_item_url(item_info_id: str) -> str:
    # GET でも直接アクセス可能な公式パラメータ名
    return f"{PORTAL_DETAIL}?procurementItemInfoId={item_info_id}"


def _portal_item_id_from_url(url: str) -> str:
    # 旧形式 (?id=) と新形式 (?procurementItemInfoId=) の両方に対応
    m = re.search(r"[?&](?:procurementItemInfoId|id)=(\d+)", url)
    return m.group(1) if m else ""


async def _portal_get_form_data(session: aiohttp.ClientSession) -> dict:
    """フォームページを GET してセッション確立 + フォームデータ（CSRF含む）を返す。"""
    raw, ct = await fetch_bytes(session, PORTAL_FORM, headers=PORTAL_HEADERS, timeout=60)
    soup = BeautifulSoup(_decode(raw, ct), "html.parser")
    form = soup.find("form", {"id": "tri_WAA0101FM01"})
    if not form:
        return {}
    data: dict = {}
    for inp in form.find_all("input"):
        name = inp.get("name", ""); val = inp.get("value", ""); itype = inp.get("type", "text")
        if not name:
            continue
        if itype in ("hidden", "text"):
            data[name] = val
        elif itype == "radio" and inp.get("checked"):
            data[name] = val
    return data


def _portal_parse_rows(soup: BeautifulSoup):
    """検索結果ページのテーブル行をパースして行情報リストを返す。"""
    rows = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        case_no = cells[0].get_text(strip=True)
        if not re.match(r"\d{19}", case_no):
            continue  # ヘッダ行等をスキップ
        title = cells[1].get_text(strip=True)
        org   = cells[2].get_text(strip=True)
        pref  = cells[3].get_text(strip=True)
        tr_html = str(tr)
        # 各種公示の itemInfoId と種別を抽出
        notices = re.findall(
            r"'procurementItemInfoId',\s*value:'(\d+)'[^)]+\),\s*'(/pps-web-biz/UAA01/OAA\d+)'",
            tr_html)
        # シンプルなパターンでも試す
        if not notices:
            item_ids = re.findall(r"'procurementItemInfoId',\s*value:'(\d+)'", tr_html)
            labels   = re.findall(r'class="[^"]*info-button[^"]*"[^>]*>([^<]+)<', tr_html)
        else:
            item_ids = [n[0] for n in notices]
            labels   = []
        labels_raw = re.findall(r'class="[^"]*info-button[^"]*"[^>]*>([^<]+)<', tr_html)
        # 公開開始日
        pub_m = re.search(r"(令和|平成|昭和)(\d{1,2})年(\d{1,2})月(\d{1,2})日公開開始", tr_html)
        published_at = _reiwa_date(pub_m.group(0).replace("公開開始", "")) if pub_m else ""
        # 落札公示があれば result の id を分離
        award_id = ""
        main_id  = ""
        for item_id, label in zip(item_ids, labels_raw):
            if "落札" in label or "rakusatu" in label:
                award_id = item_id
            elif not main_id:
                main_id = item_id
        if not main_id and item_ids:
            main_id = item_ids[0]
        if main_id:
            rows.append({
                "case_no": case_no, "title": title, "org": org, "pref": pref,
                "main_id": main_id, "award_id": award_id,
                "published_at": published_at, "labels": labels_raw,
            })
    return rows


async def scrape_portal(date_from: str = "", date_to: str = "") -> List[Dict]:
    """調達ポータルから差分を取得する。

    date_from: 取得開始日（YYYY/MM/DD 形式）。省略時は当日のみ。
    date_to:   取得終了日（YYYY/MM/DD 形式）。省略時は指定なし（上限なし）。
    build_dataset.py 側で「前回PORTAL取得日 − 1日」を計算して渡すことで
    必要最小限の差分のみを取得する。

    1セッションで GET フォーム → POST 検索 → ページング の順に取得。
    落札公示が存在する行は result_url にそのアイテム ID を記録。
    """
    from datetime import date
    results: List[Dict] = []
    seen: set = set()
    if not date_from:
        date_from = date.today().strftime("%Y/%m/%d")

    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        form_data = await _portal_get_form_data(session)
        if not form_data:
            logger.warning("調達ポータル: フォーム取得失敗")
            return results

        form_data["searchConditionBean.caseDivision"] = "0"
        form_data["searchConditionBean.publicStartDateFrom"] = date_from
        if date_to:
            form_data["searchConditionBean.publicStartDateTo"] = date_to
        form_data["OAA0102"] = "検索"
        ph = {**PORTAL_HEADERS, "Referer": PORTAL_FORM,
              "Origin": PORTAL_BASE,
              "Content-Type": "application/x-www-form-urlencoded"}

        # 初回 POST で検索実行
        await asyncio.sleep(0.7)
        async with session.post(PORTAL_SEARCH, data=form_data, headers=ph,
                                allow_redirects=True,
                                timeout=aiohttp.ClientTimeout(total=60)) as resp:
            page_html = await resp.text(encoding="utf-8", errors="replace")
            result_url_base = str(resp.url)

        # 件数抽出（例: "335 件"）
        total_m = re.search(r"(\d[\d,]*)\s*件", BeautifulSoup(page_html, "html.parser").get_text())
        total = int(total_m.group(1).replace(",", "")) if total_m else 0
        pages = max(1, -(-total // 50))  # ceiling division
        logger.info(f"調達ポータル: {total}件 / {pages}ページ (from {date_from})")

        for page in range(pages):
            if page > 0:
                await asyncio.sleep(0.7)
                async with session.get(
                    result_url_base.split("?")[0] + f"?page={page}&size=50",
                    headers={**PORTAL_HEADERS, "Referer": result_url_base},
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp2:
                    page_html = await resp2.text(encoding="utf-8", errors="replace")

            soup = BeautifulSoup(page_html, "html.parser")
            rows = _portal_parse_rows(soup)

            for row in rows:
                if row["case_no"] in seen:
                    continue
                seen.add(row["case_no"])

                url = _portal_item_url(row["main_id"])
                result_url = _portal_item_url(row["award_id"]) if row["award_id"] else ""
                tags = generate_tags(row["title"], row["org"])
                results.append({
                    "title":        row["title"],
                    "category":     "入札",  # 詳細取得後に上書き
                    "organization": row["org"],
                    "deadline":     "",           # 詳細取得後に補完
                    "published_at": row["published_at"],
                    "result_date":  "",
                    "result_url":   result_url,
                    "project_code": row["case_no"],
                    "awardee":      "",
                    "url":          url,
                    "prefecture":   row["pref"],
                    "source":       "PORTAL",
                    "amount":       "",
                    "source_category": "",
                    "summary":      "",
                    "detail":       "",
                    "tags":         ",".join(tags),
                })

    logger.info(f"調達ポータル: {len(results)}件取得")
    return results


def fetch_portal_detail(url: str) -> Dict[str, str]:
    """調達ポータル詳細ページを同期取得し、調達種別・締切・概要・添付を返す。

    CSRF + セッション管理のため GET フォーム → POST 詳細の2ステップで取得する。
    """
    import http.cookiejar
    import urllib.request as _req

    item_id = _portal_item_id_from_url(url)
    if not item_id:
        return {}

    jar = http.cookiejar.CookieJar()
    opener = _req.build_opener(_req.HTTPCookieProcessor(jar))
    opener.addheaders = [(k, v) for k, v in HEADERS.items()]

    # 1. GET フォームページ（セッション確立 + CSRF 取得）
    try:
        with opener.open(PORTAL_FORM, timeout=20) as resp:
            form_html = resp.read().decode("utf-8", "replace")
    except Exception as e:
        logger.error(f"ポータル詳細フォーム取得失敗 {url}: {e}")
        return {}

    soup_form = BeautifulSoup(form_html, "html.parser")
    csrf_inp = soup_form.find("input", {"name": "_csrf"})
    if not csrf_inp:
        return {}
    csrf = csrf_inp["value"]

    # 2. POST で詳細取得
    import urllib.parse
    post_data = urllib.parse.urlencode({
        "_csrf": csrf, "procurementItemInfoId": item_id, "SyFromFlg": "1"
    }).encode("utf-8")
    try:
        req = _req.Request(PORTAL_DETAIL, data=post_data,
                           headers={"Content-Type": "application/x-www-form-urlencoded",
                                    "Referer": PORTAL_FORM})
        with opener.open(req, timeout=25) as resp:
            detail_html = resp.read().decode("utf-8", "replace")
    except Exception as e:
        logger.error(f"ポータル詳細取得失敗 {url}: {e}")
        return {}

    soup = BeautifulSoup(detail_html, "html.parser")
    text = soup.get_text("\n", strip=True)

    # 調達種別
    choutatsushu = ""
    for m in re.finditer(r"調達種別\s*[\n\s]*(.+)", text):
        choutatsushu = m.group(1).strip()[:60]
        break

    # 公開終了日（掲載終了日）は fallback のみ。実際の入札締切は th/td から優先取得
    koukai_end = ""
    for m in re.finditer(r"公開終了日\s*[\n\s]*(.{3,30})", text):
        koukai_end = _reiwa_date(m.group(1))
        if koukai_end:
            break

    # 公告内容・分類・調達品目分類・実際の締切 を th → td から直接取得
    _EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
    raw_kouji = ""
    bunrui = ""
    hinmoku = ""
    deadline = ""
    anken_name = ""   # 調達案件名称（一覧でタイトルが取れなかった場合の補完用）
    # 入札書提出期限 / 応募期限 / 受付期限 などを優先的に取得
    _DEADLINE_TH = [
        "入札書提出期限", "入札書等提出期限", "入札書受付期限",
        "応募期限", "応募締切", "受付期限", "提出期限", "申込期限",
        "公募期限", "企画書提出期限", "入札締切",
    ]
    for th in soup.find_all("th"):
        th_text = th.get_text(strip=True)
        td = th.find_next_sibling("td") or (th.parent.find_next_sibling("tr") and
             th.parent.find_next_sibling("tr").find("td"))
        if not td:
            continue
        val = td.get_text(" ", strip=True)
        if th_text == "調達案件名称" and not anken_name:
            anken_name = re.sub(r"^【|】$", "", val.strip())
        elif th_text == "公告内容" and not raw_kouji:
            raw_kouji = _EMAIL_RE.sub("", val).strip()
        elif th_text == "分類" and not bunrui:
            bunrui = val
        elif th_text == "調達品目分類" and not hinmoku:
            hinmoku = val.strip()
        elif any(th_text.startswith(k) for k in _DEADLINE_TH) and not deadline:
            d = _reiwa_date(val)
            if d:
                deadline = d

    # th/tdで取れなかった場合、公告内容テキストから抽出を試みる
    if not deadline and raw_kouji:
        flat_kouji = re.sub(r"\s+", " ", raw_kouji)
        for kw in ["入札書提出期限", "入札書等提出期限", "応募期限", "提出期限", "申込期限", "受付期限"]:
            m = re.search(re.escape(kw) + r"[：:\s　]*(.{3,30})", flat_kouji)
            if m:
                d = _reiwa_date(m.group(1))
                if d:
                    deadline = d
                    break

    # deadline は入札書提出期限のみ。公開終了日は close_date として別途返す

    # 公告内容（定型文・記号のみはスキップ）
    _BOILER = re.compile(r"^(入札公告のとおり|添付のとおり|別紙のとおり|公募要領のとおり|仕様書のとおり|[-－])$")
    _GARBAGE_START = re.compile(r"^[口□・\s　]+")
    detail_text = ""
    if raw_kouji:
        cand = _GARBAGE_START.sub("", raw_kouji).strip()
        if cand and not _BOILER.match(cand):
            meaningful = re.sub(r"[口□・　\s]", "", cand)
            if len(meaningful) >= 5:
                # 「入　札　公　告」「公　募　要　領」のような全角スペース区切りタイトルを先頭から除去
                # 例: "入　札　公　告 次のとおり…" → "次のとおり…"
                cleaned = re.sub(
                    r"^[一-鿿　]{1,2}(　[一-鿿]{1,2}){1,5}\s*",
                    "", cand
                ).strip()
                detail_text = (cleaned or cand)  # 全文保持（AI要約側で処理）

    # 公告内容が空の場合は 調達品目分類 + 分類 から合成概要を作成
    if not detail_text and hinmoku:
        meaningful_h = re.sub(r'[口□・　\s]', '', hinmoku)
        if len(meaningful_h) >= 2:
            parts = [hinmoku]
            if bunrui and bunrui not in hinmoku:
                parts.append(f"({bunrui})")
            detail_text = "".join(parts)

    # 添付ファイル（調達資料）
    # ポータルの調達資料はGEPS(geps.go.jp)への外部リンクか、テキストに「ダウンロード」を含む
    attachments = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        label = a.get_text(" ", strip=True)
        if not href or not label:
            continue
        is_download = (
            "geps.go.jp" in href
            or re.search(r"download|\.pdf", href, re.I)
            or re.search(r"ダウンロード", label)
        )
        if is_download:
            kind = next((k for key, k in _ATTACH_KINDS if key in label), "調達資料")
            full = href if href.startswith("http") else PORTAL_BASE + href
            attachments.append({"name": label, "url": full, "kind": kind})

    return {
        "category":    _portal_category(choutatsushu),
        "detail":      detail_text,
        "budget":      "",   # ポータル本文には予算記載なし（添付PDF参照）
        "schedule":    [],
        "attachments": attachments,
        "deadline":    deadline,
        "close_date":  koukai_end,
        "choutatsushu": choutatsushu,
        "title":       anken_name,
    }


def fetch_portal_award(url: str) -> Dict[str, str]:
    """調達ポータル落札公示ページから落札者・落札日を返す。"""
    import http.cookiejar, urllib.request as _req, urllib.parse

    item_id = _portal_item_id_from_url(url)
    if not item_id:
        return {}

    jar = http.cookiejar.CookieJar()
    opener = _req.build_opener(_req.HTTPCookieProcessor(jar))
    opener.addheaders = [(k, v) for k, v in HEADERS.items()]
    try:
        with opener.open(PORTAL_FORM, timeout=20) as resp:
            form_html = resp.read().decode("utf-8", "replace")
    except Exception as e:
        logger.error(f"ポータル落札フォーム取得失敗 {url}: {e}")
        return {}
    csrf_inp = BeautifulSoup(form_html, "html.parser").find("input", {"name": "_csrf"})
    if not csrf_inp:
        return {}
    post_data = urllib.parse.urlencode({
        "_csrf": csrf_inp["value"], "procurementItemInfoId": item_id, "SyFromFlg": "1"
    }).encode("utf-8")
    try:
        req = _req.Request(PORTAL_DETAIL, data=post_data,
                           headers={"Content-Type": "application/x-www-form-urlencoded",
                                    "Referer": PORTAL_FORM})
        with opener.open(req, timeout=25) as resp:
            detail_html = resp.read().decode("utf-8", "replace")
    except Exception as e:
        logger.error(f"ポータル落札詳細取得失敗 {url}: {e}")
        return {}

    text = BeautifulSoup(detail_html, "html.parser").get_text("\n", strip=True)
    awardee = _extract_awardee(text)
    # 落札日
    result_date = ""
    for kw in ["落札日", "契約日", "決定日"]:
        m = re.search(kw + r"[^\n]{0,30}", text)
        if m:
            result_date = _reiwa_date(m.group())
            if result_date:
                break
    return {"awardee": awardee, "result_date": result_date}


# ---------------------------------------------------------------------------
# JOGMEC（エネルギー・金属鉱物資源機構）
# ---------------------------------------------------------------------------
# 連番URL /bid/bid_XXXXX.html を直接クロール。
# 一覧ページのページネーションがJS動的なため、既知の最大IDを超えた範囲を
# 毎回チェックして新着を取得する。初回バックフィル用に from_id を指定可能。

JOGMEC_BASE = "https://www.jogmec.go.jp"
# 既知の最大ID（初回バックフィル後はCSVから自動算出）
JOGMEC_BACKFILL_FROM = 65   # 2025年以降の最初のID

def _parse_jp_date(s: str) -> str:
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s or "")
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def fetch_jogmec_detail(url: str) -> Optional[Dict]:
    """JOGMECの案件詳細ページから情報を取得する（同期）。"""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
        with urllib.request.urlopen(req, timeout=15) as r:
            soup = BeautifulSoup(r.read(), "html.parser")
    except Exception as e:
        logger.debug(f"JOGMEC fetch失敗 {url}: {e}")
        return None

    title = soup.find("h1")
    title = title.get_text(strip=True) if title else ""

    # th/td テーブルから情報取得
    info: Dict[str, str] = {}
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) >= 2:
            info[cells[0].get_text(strip=True)] = cells[1].get_text(strip=True)

    published_at = _parse_jp_date(info.get("公告日", ""))
    deadline = _parse_jp_date(info.get("入札日", "") or info.get("締切日", ""))
    category_raw = info.get("種別", "")

    # 概要テキスト（ページ本文）
    main = soup.find("div", id=re.compile(r"main|contents|article", re.I)) or \
           soup.find("div", class_=re.compile(r"main|contents|article", re.I))
    detail_text = main.get_text(separator="\n", strip=True)[:2000] if main else ""

    # 添付PDF
    attachments = []
    koukoku_pdf_url = ""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/content/" in href and ".pdf" in href.lower():
            name = a.get_text(strip=True)
            kind = "公募要領" if "公募" in name else \
                   "仕様書" if "仕様" in name else \
                   "評価基準" if "評価" in name else "公告文"
            full_url = JOGMEC_BASE + href if href.startswith("/") else href
            attachments.append({"name": name, "kind": kind, "url": full_url})
            if not koukoku_pdf_url and kind == "公告文":
                koukoku_pdf_url = full_url

    # HTMLに締切がない場合、公告文PDFから抽出を試みる
    if not deadline and koukoku_pdf_url:
        try:
            import io as _io
            from pypdf import PdfReader as _PdfReader
            req2 = urllib.request.Request(koukoku_pdf_url, headers={"User-Agent": HEADERS["User-Agent"]})
            with urllib.request.urlopen(req2, timeout=20) as r2:
                pdf_data = r2.read()
            pdf_text = "\n".join(p.extract_text() or "" for p in _PdfReader(_io.BytesIO(pdf_data)).pages)
            # NFKC正規化：異体字（⽉→月、⽇→日 等）を標準形に統一
            import unicodedata as _ud
            pdf_text = _ud.normalize("NFKC", pdf_text)
            flat = re.sub(r"\s+", "", pdf_text)
            # 全角数字を半角に正規化（令和X年表記に対応）
            flat = flat.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
            _deadline_patterns = [
                # 日付が先（間に時刻数字が入る場合も .{} で許容）
                r"(令和\d+年\d{1,2}月\d{1,2}日).{0,20}公募締め?切",
                r"(令和\d+年\d{1,2}月\d{1,2}日)[^\d]{0,15}提案書.{0,6}締め?切",
                r"(令和\d+年\d{1,2}月\d{1,2}日)[^\d]{0,15}(提出期限|応募期限|受付期限)",
                # キーワードが先
                r"公募締め?切[^\d]{0,10}(令和\d+年\d{1,2}月\d{1,2}日)",
                r"提出期限.{0,30}(令和\d+年\d{1,2}月\d{1,2}日)",
                r"応募期限[^\d]{0,10}(令和\d+年\d{1,2}月\d{1,2}日)",
                r"受付期限[^\d]{0,10}(令和\d+年\d{1,2}月\d{1,2}日)",
                # 「公募期間は開始日から終了日まで/正午」（曜日・時刻を挟むため .{} で許容）
                r"公募[実施]*期間.{2,80}から(令和\d+年\d{1,2}月\d{1,2}日).{0,15}(正午|まで)",
                # 「受付期間/公募期間〜終了日」（助成金等の随時受付型）
                r"受付期間.{0,40}[～~](令和\d+年\d{1,2}月\d{1,2}日)",
                r"公募[実施]*期間[^～~]{0,20}[～~](令和\d+年\d{1,2}月\d{1,2}日)",
                # 参加意思確認書の提出期限（入札参加資格確認型）
                r"参加意思確認書.{0,100}(令和\d+年\d{1,2}月\d{1,2}日)",
                # 「交付期間：〜～令和X年Y月Z日」（フォールバック）
                r"交付期間[^\d]{0,20}[～~](令和\d+年\d{1,2}月\d{1,2}日)",
            ]
            for pat in _deadline_patterns:
                m = re.search(pat, flat)
                if m:
                    deadline = _reiwa_date(m.group(1))
                    if deadline:
                        break
            # 英語日付パターン（英語公募要領用: "on April 30, 2026"）
            if not deadline:
                _EN_MONTHS = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
                              "july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
                _en_m = re.search(
                    r"on\s*(january|february|march|april|may|june|july|august"
                    r"|september|october|november|december)\s*(\d{1,2}),?\s*(\d{4})",
                    pdf_text, re.IGNORECASE)
                if _en_m:
                    mn = _EN_MONTHS.get(_en_m.group(1).lower(), 0)
                    if mn:
                        deadline = f"{_en_m.group(3)}-{mn:02d}-{int(_en_m.group(2)):02d}"
        except Exception as e:
            logger.debug(f"JOGMEC PDF締切抽出失敗 {koukoku_pdf_url}: {e}")

    return {
        "title": title,
        "published_at": published_at,
        "deadline": deadline,
        "category_raw": category_raw,
        "detail": detail_text,
        "attachments": attachments,
    }


async def scrape_jogmec(max_id: int = 0) -> List[Dict]:
    """JOGMECの案件一覧を連番クロールで取得する。

    max_id: 既存CSVの最大JOGMEC ID。これより大きいIDのみ取得（増分）。
            0 の場合は JOGMEC_BACKFILL_FROM から現在の最大IDまで全取得。
    """
    import urllib.request
    import time as _time

    results = []
    # 増分モード: max_id+1 から先を探索。初回: BACKFILL_FROM から探索。
    start = max(max_id + 1, JOGMEC_BACKFILL_FROM)
    # 404が10連続したら打ち切り（最大IDを超えたと判断）
    consecutive_404 = 0
    num = start

    while consecutive_404 < 10:
        url = f"{JOGMEC_BASE}/bid/bid_{num:05d}.html"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
            with urllib.request.urlopen(req, timeout=10) as r:
                soup = BeautifulSoup(r.read(), "html.parser")
            consecutive_404 = 0

            title_tag = soup.find("h1")
            title = title_tag.get_text(strip=True) if title_tag else ""
            if not title:
                num += 1
                continue

            info: Dict[str, str] = {}
            for tr in soup.find_all("tr"):
                cells = tr.find_all(["th", "td"])
                if len(cells) >= 2:
                    info[cells[0].get_text(strip=True)] = cells[1].get_text(strip=True)

            published_at = _parse_jp_date(info.get("公告日", ""))
            # 2025年以前はスキップ
            if published_at and published_at < "2025-01-01":
                num += 1
                _time.sleep(0.3)
                continue

            category_raw = info.get("種別", "")
            # 企画競争・公募・参加意思確認公募 → プロポーザル / それ以外 → 入札
            category = "プロポーザル" if category_raw in (
                "企画競争", "公募", "参加意思確認公募", "総合評価落札方式"
            ) else "入札"

            tags = generate_tags(title, category_raw)

            results.append({
                "title":           title,
                "category":        category,
                "organization":    "JOGMEC（エネルギー・金属鉱物資源機構）",
                "prefecture":      "国",
                "published_at":    published_at,
                "deadline":        _parse_jp_date(info.get("入札日", "") or info.get("締切日", "")),
                "result_date":     "",
                "result_url":      "",
                "project_code":    f"JOGMEC-{num:05d}",
                "awardee":         "",
                "url":             url,
                "source":          "JOGMEC",
                "amount":          "",
                "source_category": category_raw,
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(tags),
            })
        except Exception:
            consecutive_404 += 1

        num += 1
        _time.sleep(0.3)

    logger.info(f"JOGMEC: {len(results)}件取得（bid_{start:05d}〜bid_{num-1:05d}）")
    return results


def fetch_jogmec_result_url(page_url: str) -> str:
    """JOGMEC公募ページを再取得し、結果PDFのURLを返す。なければ空文字。"""
    import urllib.request
    try:
        req = urllib.request.Request(page_url, headers={"User-Agent": HEADERS["User-Agent"]})
        with urllib.request.urlopen(req, timeout=15) as r:
            soup = BeautifulSoup(r.read(), "html.parser")
    except Exception as e:
        logger.debug(f"JOGMEC結果URL取得失敗 {page_url}: {e}")
        return ""
    for a in soup.find_all("a", href=True):
        label = a.get_text(strip=True)
        href = a["href"]
        if "結果" in label and href.endswith(".pdf"):
            base = "https://www.jogmec.go.jp"
            return href if href.startswith("http") else base + href
    return ""


def fetch_jogmec_result(pdf_url: str) -> Dict[str, str]:
    """JOGMEC結果PDFから事業者名と決定日を抽出する。"""
    import io as _io
    import unicodedata as _ud
    import urllib.request
    try:
        from pypdf import PdfReader as _PdfReader
    except ImportError:
        return {}
    try:
        req = urllib.request.Request(pdf_url, headers={"User-Agent": HEADERS["User-Agent"]})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
        text = _ud.normalize("NFKC", "\n".join(
            p.extract_text() or "" for p in _PdfReader(_io.BytesIO(data)).pages
        ))
        flat = re.sub(r"\s+", "", text).translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    except Exception as e:
        logger.debug(f"JOGMEC結果PDF取得失敗 {pdf_url}: {e}")
        return {}

    awardee = _extract_awardee(text)
    # 住所が会社名に連結して取れる場合（住所：キーワードなし）を除去
    if awardee:
        _PREF = (
            r"北海道|青森県|岩手県|宮城県|秋田県|山形県|福島県|茨城県|栃木県|群馬県|"
            r"埼玉県|千葉県|東京都|神奈川県|新潟県|富山県|石川県|福井県|山梨県|長野県|"
            r"岐阜県|静岡県|愛知県|三重県|滋賀県|京都府|大阪府|兵庫県|奈良県|和歌山県|"
            r"鳥取県|島根県|岡山県|広島県|山口県|徳島県|香川県|愛媛県|高知県|"
            r"福岡県|佐賀県|長崎県|熊本県|大分県|宮崎県|鹿児島県|沖縄県"
        )
        awardee = re.split(_PREF, awardee)[0].strip("　 、，")

    # 決定日：通知日・選定日・落札日・決定日キーワード付近の令和日付
    result_date = ""
    for kw in ["通知日", "選定日", "落札日", "決定日", "契約日"]:
        m = re.search(rf"{kw}[：:\s]*(令和\d+年\d{{1,2}}月\d{{1,2}}日)", flat)
        if m:
            result_date = _reiwa_date(m.group(1))
            if result_date:
                break
    # キーワードなければ最初の令和日付をフォールバック
    if not result_date:
        m = re.search(r"令和\d+年\d{1,2}月\d{1,2}日", flat)
        if m:
            result_date = _reiwa_date(m.group())

    return {"awardee": awardee, "result_date": result_date}


# ---------------------------------------------------------------------------
# 愛知県（あいち電子調達共同システム 物品等・県本体 groupCd=23000）
# 公開の入札情報サービス（ログイン不要・robots制限なし・サーバーHTML）から取得する。
# ---------------------------------------------------------------------------
_AICHI_BASE = "https://www.buppin.e-aichi.jp/public/"
_AICHI_GROUP = "23000"  # 愛知県本体
_AICHI_DATE = re.compile(r"令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日")


def _aichi_dec(raw: bytes) -> str:
    try:
        return raw.decode("cp932")
    except Exception:
        return raw.decode("utf-8", "replace")


def _aichi_iso(m) -> str:
    y, mo, da = int(m[0]), int(m[1]), int(m[2])
    return f"{2018 + y:04d}-{mo:02d}-{da:02d}"


def _aichi_form_fields(html: str) -> dict:
    """pubBiddingList フォームの hidden / select 既定値を辞書化。"""
    m = re.search(r'<form[^>]*action="[^"]*pubBiddingList[^"]*"[^>]*>(.*?)</form>', html, re.S)
    f = m.group(1) if m else html
    d = {}
    for inp in re.findall(r"<input[^>]+>", f):
        n = re.search(r'name="([^"]+)"', inp)
        v = re.search(r'value="([^"]*)"', inp)
        t = re.search(r'type="([^"]+)"', inp)
        if n and (not t or t.group(1).lower() in ("hidden", "text")):
            d[n.group(1)] = v.group(1) if v else ""
    for sm in re.findall(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', f, re.S):
        sel = re.search(r'<option[^>]*value="([^"]*)"[^>]*selected', sm[1])
        first = re.search(r'<option[^>]*value="([^"]*)"', sm[1])
        d[sm[0]] = sel.group(1) if sel else (first.group(1) if first else "")
    return d


def _aichi_parse_rows(html: str) -> List[Dict]:
    import urllib.parse
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        if "execBiddingDetail" not in tr:
            continue
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 6:
            continue
        args = re.search(r"openSubWinForPub\('execBiddingDetail',\s*'pubBiddingList',\s*'([^']+)'\)", tr)
        params = dict(urllib.parse.parse_qsl(args.group(1))) if args else {}
        kubun = cells[1]
        mnum = re.match(r"(\d{10,})\s*(.*)", cells[2])
        order_num = params.get("orderNum") or (mnum.group(1) if mnum else "")
        title = (mnum.group(2) if mnum else cells[2]).strip()
        if not order_num or not title:
            continue
        dept = cells[4]
        dates = _AICHI_DATE.findall(cells[5])
        pub = _aichi_iso(dates[0]) if len(dates) >= 1 else ""
        bid = _aichi_iso(dates[1]) if len(dates) >= 2 else ""
        nend = params.get("nend", "")
        url = (f"{_AICHI_BASE}pubBiddingList.do?methodName=execBiddingDetail"
               f"&nend={nend}&orderNum={order_num}&groupCd={_AICHI_GROUP}")
        rows.append({"order_num": order_num, "title": title, "kubun": kubun,
                     "dept": dept, "published_at": pub, "deadline": bid, "url": url})
    return rows


def _scrape_aichi_sync(nend: str) -> List[Dict]:
    import urllib.request, urllib.parse, http.cookiejar
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(u):
        return _aichi_dec(op.open(u, timeout=40).read())

    def post(path, data):
        body = urllib.parse.urlencode(data).encode("cp932", "replace")
        req = urllib.request.Request(
            _AICHI_BASE + path, data=body,
            headers={"User-Agent": "Mozilla/5.0",
                     "Content-Type": "application/x-www-form-urlencoded"})
        return _aichi_dec(op.open(req, timeout=40).read())

    get(_AICHI_BASE + "pubTop.do?methodName=initDisplayForPub")
    html = get(_AICHI_BASE + f"pubGroupTop.do?methodName=execOrderSearch&autonomyCd={_AICHI_GROUP}")
    fields = _aichi_form_fields(html)
    fields.update({"groupCd": _AICHI_GROUP, "nend": nend, "inputListRowLength": "100",
                   "methodName": "execApplyListRowLengthNoCheckForPub"})
    html = post("pubBiddingList.do", fields)
    rows = _aichi_parse_rows(html)
    pages = 0
    while ("execPubNext" in html) and pages < 30:
        nx = _aichi_form_fields(html)
        nx.update({"groupCd": _AICHI_GROUP, "nend": nend, "inputListRowLength": "100",
                   "methodName": "execPubNext"})
        nx["listPage"] = str(int(nx.get("listPage", "1")) + 1)
        html2 = post("pubBiddingList.do", nx)
        new = _aichi_parse_rows(html2)
        prev_last = rows[-1]["order_num"] if rows else ""
        if not new or new[-1]["order_num"] == prev_last:
            break
        rows += new
        html = html2
        pages += 1

    # 重複除去（order_num）
    seen, uniq = set(), []
    for r in rows:
        if r["order_num"] in seen:
            continue
        seen.add(r["order_num"])
        uniq.append(r)

    results = []
    for r in uniq:
        title = r["title"]
        cat = "プロポーザル" if ("プロポーザル" in title or "公募型" in title) else "入札"
        dept = r["dept"]
        results.append({
            "title":           title,
            "category":        cat,
            "organization":    ("愛知県 " + dept).strip(),
            "prefecture":      "愛知県",
            "published_at":    r["published_at"],
            "deadline":        r["deadline"],
            "result_date":     "",
            "result_url":      "",
            "project_code":    f"AICHI-{r['order_num']}",
            "awardee":         "",
            "url":             r["url"],
            "source":          "AICHI",
            "amount":          "",
            "source_category": r["kubun"],
            "summary":         "",
            "detail":          "",
            "tags":            ",".join(generate_tags(title, dept)),
        })
    logger.info(f"愛知県: {len(results)}件取得（nend={nend}）")
    return results


async def scrape_aichi(nend: str = "") -> List[Dict]:
    """愛知県本体（物品等）の入札公告を取得する。nend未指定なら現在の年度。"""
    if not nend:
        from datetime import date
        today = date.today()
        fy = today.year if today.month >= 4 else today.year - 1
        nend = str(fy)
    try:
        return await asyncio.to_thread(_scrape_aichi_sync, nend)
    except Exception as e:  # noqa: BLE001
        logger.error(f"愛知県スクレイパー例外: {e}")
        return []


# 愛知県 公募型プロポーザル（各部局が pref.aichi.jp で個別公示。まとめページから収集）
_AICHI_PROP_LIST = "https://www.pref.aichi.jp/life/5/19/66/"
_PREF_AICHI = "https://www.pref.aichi.jp"


def _scrape_aichi_proposal_sync() -> List[Dict]:
    import urllib.request
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    h = op.open(_AICHI_PROP_LIST, timeout=40).read().decode("utf-8", "replace")
    results, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', h, re.S):
        href, inner = m.group(1), m.group(2)
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", inner)).strip()
        # 記事ページ（.html）かつ 募集・委託・公募・プロポーザル・入札系のみ。
        # まとめページには補助金採択結果・協定締結・案内ページ等のノイズが多いため
        # 案件語で絞るが、一般競争入札等の入札公告も取りこぼさないよう入札系語も含める。
        if not re.search(r"\.html?($|\?)", href):
            continue
        if not re.search(r"募集|委託|公募|プロポーザル|選定|企画提案|一般競争入札|指名競争入札|総合評価|競争入札", title):
            continue
        # 先頭の【…】更新マーカーを除去
        title = re.sub(r"^【[^】]*】\s*", "", title).strip()
        if len(title) < 6:
            continue
        # 採択結果・締結報告・結果とりまとめ等は公募案件ではないため除外
        if re.search(r"採択(結果|事業|案件)|決定しました|締結しました|取りまとめ", title):
            continue
        url = href if href.startswith("http") else _PREF_AICHI + href
        if url in seen:
            continue
        seen.add(url)
        cat = "入札" if re.search(r"一般競争入札|指名競争入札|総合評価|競争入札", title) else "プロポーザル"
        slug = re.sub(r"[^A-Za-z0-9_.\-]", "_", url.split("//", 1)[-1])[-60:]
        results.append({
            "title":           title,
            "category":        cat,
            "organization":    "愛知県",
            "prefecture":      "愛知県",
            "published_at":    "",
            "deadline":        "",
            "result_date":     "",
            "result_url":      "",
            "project_code":    f"AICHI-P-{slug}",
            "awardee":         "",
            "url":             url,
            "source":          "AICHI",
            "amount":          "",
            "source_category": "公募型プロポーザル",
            "summary":         "",
            "detail":          "",
            "tags":            ",".join(generate_tags(title)),
        })
    logger.info(f"愛知県プロポーザル: {len(results)}件取得")
    return results


async def scrape_aichi_proposal() -> List[Dict]:
    """愛知県の公募型プロポーザル一覧（pref.aichi.jp）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_aichi_proposal_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"愛知県プロポーザルスクレイパー例外: {e}")
        return []


def _labeled_date_iso(text: str, labels) -> str:
    """本文から「<ラベル>[区切り] 令和X年M月D日 / YYYY年M月D日」のラベル付き日付をISOで返す。
    複数ラベルを優先順に試し、区切り(コロン/空白/改行/全角)や1-2桁・令和/西暦の揺れを吸収する。
    ラベル無しの裸の日付は拾わない（誤った日付＝履行期限等の混入を防ぐため）。"""
    for lab in labels:
        m = re.search(
            lab + r"[：:\s　（(]{0,6}(?:令和\s*(\d+)|(\d{4}))\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
            text)
        if m:
            year = 2018 + int(m.group(1)) if m.group(1) else int(m.group(2))
            return f"{year}-{int(m.group(3)):02d}-{int(m.group(4)):02d}"
    return ""


def _fetch_pref_aichi_article(url: str) -> Optional[Dict]:
    """pref.aichi.jp の記事ページ本文を取得する（プロポーザル公募の事業内容材料）。"""
    import urllib.request
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"愛知県プロポーザル詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    # 掲載日はヘッダ/メタ領域（本文スコープ外）のことがあるため decompose 前の全文から抽出
    published_at = _labeled_date_iso(soup.get_text(" ", strip=True), ["掲載日", "更新日"])
    main = (soup.find("main") or soup.find("article")
            or soup.find(id=re.compile(r"content|main", re.I))
            or soup.find(attrs={"class": re.compile(r"article|content|honbun|main", re.I)}))
    node = main if main else soup
    for tag in node.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", node.get_text("\n", strip=True))
    attachments = []
    for a in (main or soup).find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.pdf($|\?)", href, re.I):
            name = a.get_text(" ", strip=True) or "添付資料"
            full = href if href.startswith("http") else _PREF_AICHI + href
            attachments.append({"name": name, "url": full, "kind": "公募要領"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments,
            "published_at": published_at}


def fetch_aichi_detail(url: str) -> Optional[Dict]:
    """愛知県の詳細を取得する。pref.aichi.jp（プロポーザル記事）と
    buppin（入札公告詳細）の両方に対応。"""
    if "pref.aichi.jp" in url:
        return _fetch_pref_aichi_article(url)
    import urllib.request, http.cookiejar
    try:
        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        # セッション確立（詳細ページ単体でも表示できるよう先にトップを踏む）
        op.open(_AICHI_BASE + "pubTop.do?methodName=initDisplayForPub", timeout=40).read()
        html = _aichi_dec(op.open(url, timeout=40).read())
    except Exception as e:  # noqa: BLE001
        logger.error(f"愛知県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    # メインの詳細テーブル（th/td）を「ラベル: 値」で連結
    parts = []
    for tr in soup.find_all("tr"):
        th = tr.find("th")
        td = tr.find("td")
        if th and td:
            label = th.get_text(" ", strip=True)
            val = td.get_text(" ", strip=True)
            if label and val and len(val) > 1:
                parts.append(f"{label}: {val}")
    detail = "\n".join(parts)
    if len(detail) < 30:  # フォールバック：本文全体
        detail = re.sub(r"\s{2,}", " ", soup.get_text("\n", strip=True))
    return {"detail": detail[:5000], "budget": "", "schedule": [], "attachments": []}


# ---------------------------------------------------------------------------
def fetch_aichi_results(nend: str = "") -> Dict:
    """愛知県本体の入札結果一覧から {案件番号(orderNum): {awardee, amount, result_date}}
    を返す。決定事業者トラッキング用（execResult ページを一括取得）。"""
    import urllib.request, urllib.parse, http.cookiejar
    if not nend:
        from datetime import date
        today = date.today()
        nend = str(today.year if today.month >= 4 else today.year - 1)
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(u):
        return _aichi_dec(op.open(u, timeout=40).read())

    def post(path, data):
        body = urllib.parse.urlencode(data).encode("cp932", "replace")
        req = urllib.request.Request(
            _AICHI_BASE + path, data=body,
            headers={"User-Agent": "Mozilla/5.0",
                     "Content-Type": "application/x-www-form-urlencoded"})
        return _aichi_dec(op.open(req, timeout=40).read())

    def parse(html):
        out = {}
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
            if "orderNum=" not in tr:
                continue
            on = re.search(r"orderNum=([0-9]+)", tr)
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if not on or len(cells) < 6:
                continue
            rdate = _AICHI_DATE.findall(cells[4])
            result_date = _aichi_iso(rdate[0]) if rdate else ""
            res_cell = cells[5]  # 落札者 ＋ 金額
            am = re.search(r"[0-9][0-9,]*\s*円", res_cell)
            amount = am.group(0).replace(" ", "") if am else ""
            awardee = (res_cell[:am.start()] if am else res_cell).strip()
            # 落札者なし・中止・不調はスキップ
            if not awardee or re.search(r"中止|不調|取消|落札者なし|なし$", awardee):
                continue
            out[on.group(1)] = {"awardee": awardee, "amount": amount,
                                "result_date": result_date}
        return out

    try:
        get(_AICHI_BASE + "pubTop.do?methodName=initDisplayForPub")
        html = get(_AICHI_BASE +
                   f"pubGroupTop.do?methodName=execOrderSearch&autonomyCd={_AICHI_GROUP}")
        fields = _aichi_form_fields(html)
        fields.update({"groupCd": _AICHI_GROUP, "nend": nend,
                       "inputListRowLength": "100", "methodName": "execResult"})
        html = post("pubBiddingList.do", fields)
        results = parse(html)
        pages = 0
        while ("execPubNext" in html) and pages < 30:
            nx = _aichi_form_fields(html)
            nx.update({"groupCd": _AICHI_GROUP, "nend": nend,
                       "inputListRowLength": "100", "methodName": "execPubNext"})
            nx["listPage"] = str(int(nx.get("listPage", "1")) + 1)
            html2 = post("pubBiddingList.do", nx)
            new = parse(html2)
            if not new or set(new) <= set(results):
                break
            results.update(new)
            html = html2
            pages += 1
        logger.info(f"愛知県 入札結果: {len(results)}件（落札者あり・nend={nend}）")
        return results
    except Exception as e:  # noqa: BLE001
        logger.error(f"愛知県入札結果取得失敗: {e}")
        return {}


# ---------------------------------------------------------------------------
# 東京都（My TOKYO 事業者募集＝公募/委託/事業者募集）
# www.my.metro.tokyo.lg.jp の公開検索（robots許可・ログイン不要・サーバーHTML）。
# 1ページに全件（先頭はHTML、残りは articleObj のJSデータ）が含まれる。
# ※競争入札(工事/物品)は都の電子調達システム内のみでrobots禁止のため対象外。
# ---------------------------------------------------------------------------
_TOKYO_SEARCH = "https://www.my.metro.tokyo.lg.jp/business/search/?category=188514"
_TOKYO_BASE = "https://www.my.metro.tokyo.lg.jp"


def _tokyo_norm_date(s: str) -> str:
    s = (s or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def _scrape_tokyo_sync() -> List[Dict]:
    import urllib.request
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    raw = op.open(_TOKYO_SEARCH, timeout=40).read()
    h = raw.decode("utf-8", "replace")

    found = {}  # url -> {title, date}
    # (1) 先頭に描画されているアンカー
    for m in re.finditer(r'<a[^>]+href="([^"]*/w/[0-9][^"?]*)"[^>]*>(.*?)</a>', h, re.S):
        url = m.group(1).split("?")[0]
        inner = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()
        dm = re.search(r"\d{4}年\d{1,2}月\d{1,2}日", inner)
        date = _tokyo_norm_date(dm.group(0)) if dm else ""
        title = re.sub(r"\d{4}年\d{1,2}月\d{1,2}日", "", inner).strip()
        if title and len(title) > 5:
            found.setdefault(url, {"title": title, "date": date})
    # (2) 「もっと見る」用の埋め込みデータ articleObj.id/url/title/date
    for b in re.split(r"articleObj\s*=\s*new Array\(\)\s*;", h)[1:]:
        d = {}
        for k, v in re.findall(r'articleObj\.(\w+)\s*=\s*"([^"]*)"\s*;', b):
            d[k] = v
        url = (d.get("url") or "").split("?")[0]
        if url and d.get("title"):
            found.setdefault(url, {"title": d["title"].strip(),
                                   "date": _tokyo_norm_date(d.get("date", ""))})

    results = []
    for url, info in found.items():
        if "/w/" not in url:
            continue
        wid = url.rstrip("/").split("/w/")[-1]
        title = info["title"]
        cat = "入札" if "入札" in title else "プロポーザル"
        results.append({
            "title":           title,
            "category":        cat,
            "organization":    "東京都",
            "prefecture":      "東京都",
            "published_at":    info["date"],
            "deadline":        "",
            "result_date":     "",
            "result_url":      "",
            "project_code":    f"TOKYO-{wid}",
            "awardee":         "",
            "url":             url,
            "source":          "TOKYO",
            "amount":          "",
            "source_category": "事業者募集",
            "summary":         "",
            "detail":          "",
            "tags":            ",".join(generate_tags(title)),
        })
    logger.info(f"東京都: {len(results)}件取得")
    return results


async def scrape_tokyo() -> List[Dict]:
    """東京都（事業者募集・公募）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_tokyo_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"東京都スクレイパー例外: {e}")
        return []


def fetch_tokyo_detail(url: str) -> Optional[Dict]:
    """東京都 My TOKYO の記事ページから本文テキストを取得する（事業内容の材料）。"""
    import urllib.request
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"東京都詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    # 記事本文領域を優先的に抽出
    main = (soup.find("main") or soup.find("article")
            or soup.find(attrs={"class": re.compile(r"article|content|body|w-article", re.I)}))
    node = main if main else soup
    # ナビ・スクリプト等を除去
    for tag in node.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", node.get_text("\n", strip=True))
    # 添付PDFリンク
    attachments = []
    for a in (main or soup).find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.pdf($|\?)", href, re.I):
            name = a.get_text(" ", strip=True) or "添付資料"
            full = href if href.startswith("http") else _TOKYO_BASE + href
            attachments.append({"name": name, "url": full, "kind": "公募要領"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments}


# ---------------------------------------------------------------------------
# 大阪府（電子契約システム＝旧CALS/EC / プロポーザルは公式サイト静的ページ）
# ---------------------------------------------------------------------------
_OSAKA_BASE = "https://www.e-nyusatsu.pref.osaka.jp"
_OSAKA_EB = _OSAKA_BASE + "/CALS/Publish/EbController"
# 契約区分: 00=建設工事 01=測量・建設コンサル等 02=委託役務 03=物品
_OSAKA_KEIYAKU_KBN = {"00": "建設工事", "01": "測量・建設コンサルタント等",
                       "02": "委託役務", "03": "物品"}
_OSAKA_DATE = re.compile(r"令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日")
_OSAKA_DATE_SLASH = re.compile(r"R(\d+)/(\d+)/(\d+)")


def _osaka_iso(y, mo, da) -> str:
    return f"{2018 + int(y):04d}-{int(mo):02d}-{int(da):02d}"


def _osaka_session():
    """セッションを確立したopenerを返す（検索・詳細どちらの前にも必要）。"""
    import urllib.request, http.cookiejar
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url, ref=None):
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0", **({"Referer": ref} if ref else {})})
        return op.open(req, timeout=40).read().decode("shift_jis", "replace")

    def post(url, data, ref=None):
        import urllib.parse
        body = urllib.parse.urlencode(data).encode("shift_jis", "replace")
        req = urllib.request.Request(
            url, data=body,
            headers={"User-Agent": "Mozilla/5.0",
                     "Content-Type": "application/x-www-form-urlencoded",
                     **({"Referer": ref} if ref else {})})
        return op.open(req, timeout=40).read().decode("shift_jis", "replace")

    top_url = _OSAKA_EB + "?Shori=KokokuInfo"
    get(top_url)
    wp_url = _OSAKA_BASE + "/CALS/Publish/ebidmlit/jsp/common/EbKokokuCertificate.jsp"
    get(wp_url, ref=top_url)
    # 検索フォーム画面を一度経由する（未経由だとシステムエラーになる）
    post(_OSAKA_EB, {"clientKind": "0", "screenID": "CPC000",
                      "omeParameterID": "P001CPCS01", "RandomRequestKey": ""}, ref=wp_url)
    return get, post


def _osaka_parse_rows(html: str) -> List[Dict]:
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        m = re.search(r"open_tenpu\('([^']+)'\)", tr)
        if not m:
            continue
        anken_no = m.group(1)
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        cells = [c for c in cells if c is not None]
        if len(cells) < 8:
            continue
        # [No, 部署, 件名, 入札方式, 業種/工種, 所在地, 公告日, 締切日, ...]
        dept, title, method, kind, addr = cells[1], cells[2], cells[3], cells[4], cells[5]
        pub_m = _OSAKA_DATE_SLASH.search(cells[6])
        dl_m = _OSAKA_DATE_SLASH.search(cells[7])
        pub = _osaka_iso("20" + pub_m.group(1) if len(pub_m.group(1)) == 1 else pub_m.group(1),
                          pub_m.group(2), pub_m.group(3)) if pub_m else ""
        deadline = _osaka_iso("20" + dl_m.group(1) if len(dl_m.group(1)) == 1 else dl_m.group(1),
                               dl_m.group(2), dl_m.group(3)) if dl_m else ""
        if not title or not anken_no:
            continue
        rows.append({"anken_no": anken_no, "title": title, "dept": dept,
                      "method": method, "kind": kind, "addr": addr,
                      "published_at": pub, "deadline": deadline})
    return rows


def _scrape_osaka_sync() -> List[Dict]:
    from datetime import date
    today = date.today()
    nendo = str(today.year if today.month >= 4 else today.year - 1)
    try:
        get, post = _osaka_session()
    except Exception as e:  # noqa: BLE001
        logger.error(f"大阪府セッション確立失敗: {e}")
        return []

    results = []
    seen = set()
    for kbn, kbn_label in _OSAKA_KEIYAKU_KBN.items():
        try:
            data = {
                "screenID": "CPC000", "omeParameterID": "P002CPCS02", "clientKind": "0",
                "searchKensakuDispKbn": "0", "searchKeiyakuKbn": kbn, "keiyakuKbn": kbn,
                "searchOrganNumber": "00", "searchDepartNumber": "", "searchOfficeNumber": "",
                "searchProjectName": "", "searchNyusatsuNo": "", "searchNyusatsuKbn": "",
                "searchGyoshu": "", "searchAddress": "",
                "searchKokokuStartDate": "", "searchKokokuEndDate": "",
                "searchKaisatsuStartDate": "", "searchKaisatsuEndDate": "",
                "searchNyusatsuStartDate": "", "searchNyusatsuEndDate": "",
                # 各契約区分の現在公告中を全件取得（50件だと大量に取りこぼす。実測で
                # 委託役務667/測量509/建設457/物品209＝計約1800件。頭打ち防止に大きめ）
                "searchShowRange": "3000", "showRange": "3000",
                "selectNendoIndex": "2", "searchNendo": nendo, "serverDateGengo": "令和",
            }
            html = post(_OSAKA_EB, data, ref=_OSAKA_EB)
        except Exception as e:  # noqa: BLE001
            logger.error(f"大阪府検索失敗（{kbn_label}）: {e}")
            continue
        for r in _osaka_parse_rows(html):
            if r["anken_no"] in seen:
                continue
            seen.add(r["anken_no"])
            title = r["title"]
            results.append({
                "title":           title,
                "category":        "入札",
                "organization":    ("大阪府 " + r["dept"]).strip(),
                "prefecture":      "大阪府",
                "published_at":    r["published_at"],
                "deadline":        r["deadline"],
                "result_date":     "",
                # 案件番号は入札公告と共通のため、結果ページも同じURLで参照できる
                # （check_results.py が result_url を見て fetch_osaka_result を呼ぶ）
                "result_url":      f"{_OSAKA_EB}?ankenNo={r['anken_no']}",
                "project_code":    f"OSAKA-{r['anken_no']}",
                "awardee":         "",
                "url":             f"{_OSAKA_EB}?ankenNo={r['anken_no']}",
                "source":          "OSAKA",
                "amount":          "",
                "source_category": f"{kbn_label}/{r['kind']}",
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title, r["dept"])),
            })
        # 負荷対策：区分ごとに間隔を空ける
        import time as _time
        _time.sleep(1.0)
    logger.info(f"大阪府 入札: {len(results)}件取得")
    return results


async def scrape_osaka() -> List[Dict]:
    """大阪府電子契約システム（旧CALS/EC）の入札公告を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_osaka_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"大阪府スクレイパー例外: {e}")
        return []


def fetch_osaka_detail(url: str) -> Optional[Dict]:
    """大阪府 入札案件の詳細（案件番号から）を取得する。添付書類はPDFのみ保存対象にする。"""
    import urllib.parse
    q = urllib.parse.urlparse(url).query
    anken_no = dict(urllib.parse.parse_qsl(q)).get("ankenNo", "")
    if not anken_no:
        return None
    try:
        get, post = _osaka_session()
        html = post(_OSAKA_EB, {"screenID": "CPC000", "omeParameterID": "P009CPCS09",
                                 "clientKind": "0", "projectNumber": anken_no}, ref=_OSAKA_EB)
    except Exception as e:  # noqa: BLE001
        logger.error(f"大阪府詳細取得失敗 {url}: {e}")
        return None
    # 「項目名」「値」の対を本文テキストとして連結
    pairs = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        cells = [c for c in cells if c]
        if len(cells) == 2:
            pairs.append(f"{cells[0]}: {cells[1]}")
    detail = "\n".join(pairs)
    # 添付（拡張子.pdfのみR2対象。docx/xlsx等はリンクのみ保持）
    attachments = []
    for m in re.finditer(r"moveDownLoad\('([^']+)',\s*'([^']+)'\)", html):
        data_name, disp_name = m.group(1), m.group(2)
        attachments.append({
            "name": disp_name, "kind": "公募要領",
            "url": f"{_OSAKA_EB}?omeParameterID=P009DOWN02&dataName={data_name}"
                   f"&dispFileName={urllib.parse.quote(disp_name)}&screenID=CPC000",
        })
    return {"detail": detail[:6000], "budget": "", "schedule": [], "attachments": attachments}


def fetch_osaka_result(url: str) -> Dict[str, str]:
    """大阪府 入札結果（落札者）ページから決定事業者を返す（NEDO方式：result_url→fetch→awardee）。

    案件番号は入札公告と共通のため、fetch_osaka_detail と同じURL(?ankenNo=)をそのまま使う。
    未決定（「確認中」等）の場合は awardee 空文字を返す。
    """
    import urllib.parse
    q = urllib.parse.urlparse(url).query
    anken_no = dict(urllib.parse.parse_qsl(q)).get("ankenNo", "")
    if not anken_no:
        return {}
    try:
        get, post = _osaka_session()
        html = post(_OSAKA_EB, {"screenID": "CPC000", "omeParameterID": "P005CPCS05",
                                 "clientKind": "0", "projectNumber": anken_no}, ref=_OSAKA_EB)
    except Exception as e:  # noqa: BLE001
        logger.error(f"大阪府結果取得失敗 {url}: {e}")
        return {}
    pairs = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        cells = [c for c in cells if c]
        if len(cells) == 2:
            pairs[cells[0].replace(" ", "").strip()] = cells[1]
    awardee = ""
    for key in pairs:
        if key.startswith("落札企業名称") or key.startswith("落札者"):
            val = pairs[key].replace("&nbsp;", "").strip()
            if val:
                awardee = val
            break
    result_date = ""
    kaisatsu = pairs.get("開札日時", "")
    m = _OSAKA_DATE.search(kaisatsu)
    if m:
        result_date = _osaka_iso(m.group(1), m.group(2), m.group(3))
    return {"awardee": awardee, "result_date": result_date} if awardee else {}


# 大阪府 公募型プロポーザル（公式サイトに静的一覧。案件は各部局配下の個別ページ）
_OSAKA_PROP_LIST = "https://www.pref.osaka.lg.jp/o040100/keiyaku_2/e-nyuusatsu/puropo.html"
_PREF_OSAKA = "https://www.pref.osaka.lg.jp"


# 一覧の各行: 「令和X年M月D日～令和Y年M月D日：<a>案件名</a>：発注室・課」
# （区切りは全角コロン／全角スペースの両方がある）。公募期間の開始=掲載日、終了=締切。
_OSAKA_PROP_LI = re.compile(
    r'<li>\s*令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日'          # 公募開始
    r'\s*[〜～]\s*令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日'      # 公募終了
    r'\s*[:：]?\s*<a href="([^"]+)">([^<]+)</a>'                    # 案件リンク
    r'\s*[:：　]?\s*([^<]*)',                                       # 発注室・課（任意）
    re.S)


def _scrape_osaka_proposal_sync() -> List[Dict]:
    import urllib.request
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    try:
        html = op.open(_OSAKA_PROP_LIST, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"大阪府プロポーザル一覧取得失敗: {e}")
        return []
    results = []
    seen = set()
    # 案件名テキストのキーワードで絞るのではなく、一覧の<li>（公募期間つき）を直接パースする。
    # 案件名に「プロポーザル」等の語が無い案件（大多数）を取りこぼしていた不具合の修正。
    for m in _OSAKA_PROP_LI.finditer(html):
        sy, sm, sd, ey, em, ed, href, title, dept = m.groups()
        full = href if href.startswith("http") else _PREF_OSAKA + href
        if full in seen or "puropo.html" in full:
            continue
        seen.add(full)
        title = re.sub(r"^【[^】]*】\s*", "", title).strip()
        # 過去分アーカイブへのハブリンク（案件ではない）は除外
        if "公示日以前" in title or "puropo_" in full:
            continue
        dept = re.sub(r"\s+", "", dept or "").strip()
        pub = _osaka_iso(sy, sm, sd)          # _osaka_iso が令和→西暦変換する
        deadline = _osaka_iso(ey, em, ed)
        slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-") or str(len(seen))
        results.append({
            "title":           title,
            "category":        "プロポーザル",
            "organization":    ("大阪府 " + dept).strip(),
            "prefecture":      "大阪府",
            "published_at":    pub,
            "deadline":        deadline,
            "result_date":     "",
            "result_url":      "",
            "project_code":    f"OSAKA-P-{slug}",
            "awardee":         "",
            "url":             full,
            "source":          "OSAKA",
            "amount":          "",
            "source_category": "",
            "summary":         "",
            "detail":          "",
            "tags":            ",".join(generate_tags(title, dept)),
        })
    logger.info(f"大阪府 プロポーザル: {len(results)}件取得")
    return results


async def scrape_osaka_proposal() -> List[Dict]:
    try:
        return await asyncio.to_thread(_scrape_osaka_proposal_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"大阪府プロポーザルスクレイパー例外: {e}")
        return []


def _fetch_pref_osaka_article(url: str) -> Optional[Dict]:
    """pref.osaka.lg.jp の記事ページ本文を取得する（プロポーザル公募の事業内容材料）。"""
    import urllib.request
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"大阪府プロポーザル詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    main = (soup.find("main") or soup.find("article")
            or soup.find(id=re.compile(r"content|main", re.I))
            or soup.find(attrs={"class": re.compile(r"article|content|honbun|main", re.I)}))
    node = main if main else soup
    for tag in node.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", node.get_text("\n", strip=True))
    # 一覧で件名が壊れた場合の補正用に、記事の正式タイトル(h1優先、無ければtitleタグ)を返す
    page_title = ""
    h1 = soup.find("h1")
    if h1:
        page_title = h1.get_text(" ", strip=True)
    if not page_title:
        tt = soup.find("title")
        if tt:
            page_title = re.split(r"[／/｜|]", tt.get_text(" ", strip=True))[0].strip()
    attachments = []
    for a in (main or soup).find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.pdf($|\?)", href, re.I):
            name = a.get_text(" ", strip=True) or "添付資料"
            full = href if href.startswith("http") else _PREF_OSAKA + href
            attachments.append({"name": name, "url": full, "kind": "公募要領"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments,
            "title": page_title}


def fetch_osaka_proposal_detail(url: str) -> Optional[Dict]:
    return _fetch_pref_osaka_article(url)


# ---------------------------------------------------------------------------
# 福岡県（公式サイト内「入札・公募」一覧。入札・プロポーザル・企画提案が混在）
# ---------------------------------------------------------------------------
_FUKUOKA_BASE = "https://www.pref.fukuoka.lg.jp"
_FUKUOKA_LIST = _FUKUOKA_BASE + "/bid/index.php"
# 「落札者の公示」系タイトルの判定（表記が多様：「等について」「＜＞」「(落札者の公示について)「...」」等）。
# 結果発表であり応募できる案件ではないため、一覧から除外する判定にはこのラベル一致だけを使う。
_FUKUOKA_AWARD_LABEL = re.compile(r"落札者の公示|契約の相手方の公示|契約者の公示")


def _fukuoka_award_contract_name(title: str) -> str:
    """「落札者の公示」系タイトルから契約名称を取り出す（無ければ空文字）。

    括弧の種類（「」（）()＜＞）がタイトルにより異なるため順に試す。契約名自体に
    括弧が入れ子で含まれることがある（例:「...業務委託契約（筑後）」）ため、最初の
    開き括弧から最後の閉じ括弧までを貪欲マッチで取る。ラベル自体を囲むだけの括弧
    （"落札者の公示について"等）は除外する。
    """
    for lb, rb in (("「", "」"), ("（", "）"), ("(", ")"), ("＜", "＞")):
        m = re.search(re.escape(lb) + r"(.+)" + re.escape(rb), title)
        if m and "の公示" not in m.group(1):
            return m.group(1).strip()
    return ""


def _scrape_fukuoka_sync(max_pages: int = 12) -> List[Dict]:
    import urllib.request
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    results = []
    seen = set()
    for page in range(1, max_pages + 1):
        url = _FUKUOKA_LIST if page == 1 else f"{_FUKUOKA_LIST}?page={page}"
        try:
            html = get(url)
        except Exception as e:  # noqa: BLE001
            logger.error(f"福岡県一覧取得失敗（page={page}）: {e}")
            break
        blocks = re.findall(
            r'<span class="article_date_y">([^<]+)</span>\s*'
            r'<span class="article_date_md">([^<]+)</span>.*?'
            r'<span class="article_title"><a href="([^"]+)">([^<]+)</a></span>\s*'
            r'<span class="article_section">(?:<a[^>]*>([^<]*)</a>)?',
            html, re.S)
        if not blocks:
            break
        new_count = 0
        for y, md, href, title, org in blocks:
            full = href if href.startswith("http") else _FUKUOKA_BASE + href
            if full in seen:
                continue
            seen.add(full)
            new_count += 1
            title = title.strip()
            if _FUKUOKA_AWARD_LABEL.search(title):
                # 「落札者の公示」等は結果発表であり応募できる案件ではないため、
                # 通常の案件一覧には含めない（決定事業者は fetch_fukuoka_results で別途取得）。
                continue
            m = re.match(r"(\d+)月(\d+)日", md.strip())
            pub = f"{y.strip().rstrip('年')}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else ""
            cat = "プロポーザル" if re.search(r"プロポーザル|企画提案|企画競争", title) else "入札"
            slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-")
            results.append({
                "title":           re.sub(r"^【[^】]*】\s*", "", title),
                "category":        cat,
                "organization":    ("福岡県 " + (org or "").strip()).strip(),
                "prefecture":      "福岡県",
                "published_at":    pub,
                "deadline":        "",
                "result_date":     "",
                "result_url":      "",
                "project_code":    f"FUKUOKA-{slug}",
                "awardee":         "",
                "url":             full,
                "source":          "FUKUOKA",
                "amount":          "",
                "source_category": "",
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title, org or "")),
            })
        if new_count == 0:
            break
        import time as _time
        _time.sleep(0.8)
    logger.info(f"福岡県: {len(results)}件取得")
    return results


async def scrape_fukuoka() -> List[Dict]:
    """福岡県公式サイト「入札・公募」一覧（/bid/）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_fukuoka_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"福岡県スクレイパー例外: {e}")
        return []


def fetch_fukuoka_detail(url: str) -> Optional[Dict]:
    """福岡県 入札・公募 個別記事ページの本文を取得する。"""
    import urllib.request
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"福岡県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    main = (soup.find("main") or soup.find("article")
            or soup.find(id=re.compile(r"content|main", re.I))
            or soup.find(attrs={"class": re.compile(r"article|content|honbun|main", re.I)}))
    node = main if main else soup
    for tag in node.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", node.get_text("\n", strip=True))
    attachments = []
    for a in (main or soup).find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.pdf($|\?)", href, re.I):
            name = a.get_text(" ", strip=True) or "添付資料"
            full = href if href.startswith("http") else _FUKUOKA_BASE + href
            attachments.append({"name": name, "url": full, "kind": "公募要領"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments}


def _fukuoka_wareki_iso(text: str) -> str:
    m = re.search(r"令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", text)
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{2018 + y:04d}-{mo:02d}-{d:02d}"


# ---------------------------------------------------------------------------
# 三重県（公式サイトのカテゴリ別一覧ページ。1ページで全部署の案件を横断表示）
# ---------------------------------------------------------------------------
_MIE_BASE = "https://www.pref.mie.lg.jp"
_MIE_CATEGORIES = [
    ("/app/nyusatsu/nyusatsu/00006836/0/0", "プロポーザル"),  # 業務委託：企画提案コンペ公告
    ("/app/nyusatsu/nyusatsu/00006837/0/0", "プロポーザル"),  # 印刷・その他：企画提案コンペ公告
    ("/app/nyusatsu/nyusatsu/00006835/0/1", "プロポーザル"),  # その他関連情報：企画提案コンペ公告
    ("/app/nyusatsu/nyusatsu/00006828/0/1", "入札"),          # その他関連情報：一般競争入札公告
]


def _mie_wareki_iso(text: str) -> str:
    m = re.search(r"令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{2018 + y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"平成\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{1988 + y:04d}-{mo:02d}-{d:02d}"
    return ""


def _mie_wareki_iso(text: str) -> str:
    # 「令和08年7月24日」「平成31年4月18日」→ ISO。開札/公告日で降順ソートに使う。
    m = re.search(r"(令和|平成)\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", text)
    if not m:
        return ""
    era, y, mo, da = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
    year = (2018 + y) if era == "令和" else (1988 + y)
    return f"{year:04d}-{mo:02d}-{da:02d}"


def _scrape_mie_sync(max_pages: int = 40, window_days: int = 90) -> List[Dict]:
    # 三重の一覧は開札/公告日の新しい順に全履歴（現役カテゴリは500件超）を返すため、
    # 固定ページ数ではなく日付ウィンドウで打ち切る。直近window_days日以内〜将来日
    # （＝現在公告中＋直近分）のみ収集し、古い行が出たらそのカテゴリを停止する。
    from datetime import date, timedelta
    cutoff_iso = (date.today() - timedelta(days=window_days)).isoformat()
    import urllib.request
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    results = []
    seen = set()
    for path, cat in _MIE_CATEGORIES:
        stop = False
        for page in range(1, max_pages + 1):
            if stop:
                break
            url = _MIE_BASE + path + ("/" if page == 1 else f"?SPI={page}")
            try:
                html = get(url)
            except Exception as e:  # noqa: BLE001
                logger.error(f"三重県一覧取得失敗（{path} page={page}）: {e}")
                break
            rows = re.findall(
                r'<td class="date-a">([^<]+)</td>\s*'
                r'<td class="news-a"><a href="([^"]+)">([^<]+)</a></td>\s*'
                r'<td class="from-a"><a[^>]*>([^<]+)</a>',
                html, re.I)
            if not rows:
                break
            new_count = 0
            for d, href, title, org in rows:
                ld = _mie_wareki_iso(d)
                # 開札/公告日が新しい順。ウィンドウより古い行に達したら以降は全て古い→停止
                if ld and ld < cutoff_iso:
                    stop = True
                    break
                full = href if href.startswith("http") else _MIE_BASE + href
                if full in seen:
                    continue
                seen.add(full)
                new_count += 1
                title = title.strip()
                slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-")
                results.append({
                    "title":           title,
                    "category":        cat,
                    "organization":    ("三重県 " + (org or "").strip()).strip(),
                    "prefecture":      "三重県",
                    # 一覧の日付列は「コンペ実施日」等であり公告日ではないため、
                    # published_at は詳細ページ（fetch_mie_detail）の値で補完する
                    "published_at":    "",
                    "deadline":        "",
                    "result_date":     "",
                    "result_url":      "",
                    "project_code":    f"MIE-{slug}",
                    "awardee":         "",
                    "url":             full,
                    "source":          "MIE",
                    "amount":          "",
                    "source_category": "",
                    "summary":         "",
                    "detail":          "",
                    "tags":            ",".join(generate_tags(title, org or "")),
                })
            if new_count == 0:
                break
            import time as _time
            _time.sleep(0.8)
    logger.info(f"三重県: {len(results)}件取得")
    return results


async def scrape_mie() -> List[Dict]:
    """三重県公式サイト 入札・企画提案コンペ カテゴリ別一覧を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_mie_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"三重県スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 汎用 efftis「入札情報公開システム(/ppi/pub)」スクレイパー。三重・京都・富山が同ベンダー
# （efftis）。既存の各県スクレイパーは入札(建設含む)を取りこぼしているためこれで補完する。
# 特長: 一覧に全情報（案件名/発注機関/工種/入札方式/受付締切）があり詳細フェッチ不要。
# 詳細は deep-link URL（pub?s=P002&a=4&ankenNo=…）で公式ページに直接飛べる。
# フロー: GET(フォーム) → POST s=P004,a=4(1ページ目) → 以降 s=P002,a=3 で次ページ(セッション制)。
# cfg: base(str,/ppi/pub), pref(str), source(str), max_pages(int)
# ---------------------------------------------------------------------------
_EFFTIS_ANKEN_RE = re.compile(r"openDetailBidding\('pub\?s=P002&a=4&ankenNo=(\d+)'\)")


def _efftis_collect_fields(form_html: str) -> Dict[str, str]:
    f = {}
    for m in re.finditer(r'<input[^>]+name="([^"]+)"[^>]*>', form_html, re.I):
        tag, n = m.group(0), m.group(1)
        ty = (re.search(r'type="([^"]+)"', tag) or ["", "text"])[1]
        if ty.lower() in ("checkbox", "radio") and "checked" not in tag.lower():
            continue
        f[n] = (re.search(r'value="([^"]*)"', tag) or ["", ""])[1]
    for m in re.finditer(r'<select[^>]+name="([^"]+)"[^>]*>(.*?)</select>', form_html, re.S | re.I):
        sel = re.search(r'<option[^>]*selected[^>]*value="([^"]*)"', m.group(2)) or re.search(r'<option[^>]*value="([^"]*)"', m.group(2))
        f[m.group(1)] = sel.group(1) if sel else ""
    return f


def _efftis_wareki(text: str) -> str:
    # 「令和8/7/14」「R8/7/17」「令和08/07/14」→ ISO
    m = re.search(r"(?:令和|R)\s*(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})", text)
    if not m:
        return ""
    return f"{2018 + int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _efftis_clean_title(s: str) -> str:
    import html as _html
    s = _html.unescape(s)
    s = re.sub(r"【[^】]*】", "", s)                       # 【7/17 …追加・差替】等の修正メモ
    s = re.sub(r"※[^。]*。", "", s)                        # ※…しました。
    # 「令和8年7月17日；予定価格の公表及び添付ファイルを修正しました。」等の日付つき告知
    s = re.sub(r"令和\d+年\d+月\d+日[；;：:].*?(?:しました|します)。", "", s)
    s = re.sub(r"(?:予定価格の公表|添付ファイル|質問(?:回答)?)[^。]*(?:しました|します|追加・差替)。?", "", s)
    s = re.sub(r"公告日\s*(?:令和|R)?\s*\d+\s*/\s*\d+\s*/\s*\d+", "", s)  # 公告日 令和8/7/14
    return re.sub(r"\s+", " ", s).strip()


def _scrape_efftis_ppi(cfg: Dict) -> List[Dict]:
    import urllib.request
    import urllib.parse
    import http.cookiejar
    import time as _time
    import html as _html

    base, pref, source = cfg["base"], cfg["pref"], cfg["source"]
    max_pages = cfg.get("max_pages", 40)

    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", "Mozilla/5.0"), ("Referer", base)]

    def postf(fields):
        body = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(base, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        return op.open(req, timeout=60).read().decode("cp932", "replace")

    results: List[Dict] = []
    seen = set()
    try:
        form = op.open(base, timeout=40).read().decode("cp932", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"{pref}(efftis)フォーム取得失敗: {e}")
        return results

    fields = _efftis_collect_fields(form)
    fields["s"], fields["a"] = "P004", "4"     # 入札予定(公告)検索
    for page in range(1, max_pages + 1):
        try:
            html_res = postf(fields)
        except Exception as e:  # noqa: BLE001
            logger.error(f"{pref}(efftis)検索失敗（page={page}）: {e}")
            break
        rows = [tr for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html_res, re.S | re.I)
                if "openDetailBidding" in tr]
        for tr in rows:
            am = _EFFTIS_ANKEN_RE.search(tr)
            if not am:
                continue
            anken = am.group(1)
            if anken in seen:
                continue
            seen.add(anken)
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)
            cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", c))).replace("\xa0", " ").strip() for c in tds]
            # 列: [No, 発注機関+施行番号, 修正, 案件名, 入札方式, 工種, 格付, 受付期間, 状態]
            org_raw = cells[1] if len(cells) > 1 else ""
            org = re.sub(r"\d{4,}$", "", org_raw).strip() or pref
            name_cell = cells[3] if len(cells) > 3 else ""
            title = _efftis_clean_title(name_cell)
            method = cells[4] if len(cells) > 4 else ""
            gyoshu = cells[5] if len(cells) > 5 else ""
            recv = cells[7] if len(cells) > 7 else ""
            pubm = re.search(r"公告日\s*((?:令和|R)?\s*\d+\s*/\s*\d+\s*/\s*\d+)", name_cell)
            published = _efftis_wareki(pubm.group(1)) if pubm else ""
            deadline = ""
            if "～" in recv:
                deadline = _efftis_wareki(recv.split("～", 1)[1])
            elif recv:
                deadline = _efftis_wareki(recv)
            if not title:
                continue
            results.append({
                "title":           title,
                "category":        "プロポーザル" if "プロポ" in method else "入札",
                "organization":    (pref + " " + org).strip() if not org.startswith(pref) else org,
                "prefecture":      pref,
                "published_at":    published,
                "deadline":        deadline,
                "result_date":     "",
                "result_url":      "",
                "project_code":    f"{source}-EFFTIS-{anken}",
                "awardee":         "",
                "url":             f"{base}?s=P002&a=4&ankenNo={anken}",
                "source":          source,
                "amount":          "",
                "source_category": gyoshu or method,
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title, org)),
            })
        # 次ページ判定
        pg = re.search(r"(\d+)\s*/\s*(\d+)\s*ページ", html_res)
        if not pg or int(pg.group(1)) >= int(pg.group(2)):
            break
        fields = _efftis_collect_fields(html_res)
        fields["s"], fields["a"] = "P002", "3"   # 次ページ
        _time.sleep(0.25)
    logger.info(f"{pref} 入札(efftis電子調達): {len(results)}件取得")
    return results


_MIE_EFFTIS_CFG = {
    "base": "https://mie.efftis.jp/24000/ppi/pub",
    "pref": "三重県", "source": "MIE", "max_pages": 40,
}


async def scrape_mie_efftis() -> List[Dict]:
    """三重県 入札予定（公告）を三重県電子調達システム(efftis)から取得する。

    既存の三重スクレイパーは企画提案コンペ中心で建設等の入札が欠落していたため補完。
    一覧に全項目があり詳細取得不要。URLは案件個別のdeep-link（公式ページに直接遷移可）。
    """
    try:
        return await asyncio.to_thread(_scrape_efftis_ppi, _MIE_EFFTIS_CFG)
    except Exception as e:  # noqa: BLE001
        logger.error(f"三重県efftisスクレイパー例外: {e}")
        return []


def fetch_mie_detail(url: str) -> Optional[Dict]:
    """三重県 入札・公募 個別記事ページの本文を取得する。"""
    import urllib.request
    # 電子調達(efftis)の案件は一覧で情報確定済み・詳細はdeep-link。スキップ。
    if "efftis.jp" in url:
        return None
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"三重県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find(id="center-contents") or soup
    # 公告日はmain-textの外（タイトル直前のヘッダー部）に単独で置かれているため、
    # containerの本文中で最初に出現する日付を公告日とみなす
    published_at = _mie_wareki_iso(container.get_text("\n", strip=True))
    main = container.find(attrs={"class": re.compile(r"main-text", re.I)}) or container
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for li in container.find_all("li"):
        a = li.find("a", href=True)
        if a and re.search(r"\.pdf($|\?)", a["href"], re.I):
            name = li.get_text(" ", strip=True).split("(")[0].strip() or "添付資料"
            full = a["href"] if a["href"].startswith("http") else _MIE_BASE + a["href"]
            attachments.append({"name": name, "url": full, "kind": "公募要領"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments,
            "published_at": published_at}


def _title_bigrams(s: str) -> set:
    """タイトル類似度判定用のbigram集合を作る（公告と結果でタイトルの言い回しが微妙に
    異なる場合でも高いJaccard類似度が出るよう、空白のみ除去して2文字組を取る）。"""
    s = re.sub(r"[\s　]", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


# 三重県 企画提案コンペ結果一覧（公告一覧と同じ構造。プロポーザル系のみ対応、
# 一般競争入札結果は落札者名がPDF添付のみのため対象外）
_MIE_RESULT_CATEGORIES = [
    "/app/nyusatsu/nyusatsu/00006836/1/0",  # 業務委託：企画提案コンペ結果
    "/app/nyusatsu/nyusatsu/00006837/1/0",  # 印刷・その他：企画提案コンペ結果
]


def fetch_mie_results(max_pages: int = 5) -> List[Dict]:
    """三重県 企画提案コンペの結果一覧を一括取得する。

    案件ごとに独立した結果記事のため、決定事業者と公告記事の突合はタイトルの
    bigram類似度で行う（check_results.py側）。返り値の各要素は
    {"title", "bigrams", "awardee", "result_date"}。
    """
    import urllib.request
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    results = []
    for path in _MIE_RESULT_CATEGORIES:
        for page in range(1, max_pages + 1):
            url = _MIE_BASE + path + ("/" if page == 1 else f"?SPI={page}")
            try:
                html = get(url)
            except Exception as e:  # noqa: BLE001
                logger.error(f"三重県結果一覧取得失敗（{path} page={page}）: {e}")
                break
            rows = re.findall(r'<td class="news-a"><a href="([^"]+)">([^<]+)</a>', html)
            if not rows:
                break
            for href, title in rows:
                full = href if href.startswith("http") else _MIE_BASE + href
                try:
                    dhtml = get(full)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"三重県結果詳細取得失敗 {full}: {e}")
                    continue
                soup = BeautifulSoup(dhtml, "html.parser")
                container = soup.find(id="center-contents") or soup
                main = container.find(attrs={"class": re.compile(r"main-text", re.I)}) or container
                text = main.get_text("\n", strip=True)
                m = re.search(r"最優秀(?:受託候補者|提案者)\s*\n([^\n]+)", text)
                awardee = m.group(1).strip() if m else ""
                if not awardee:
                    continue
                title = title.strip()
                results.append({
                    "title": title,
                    "bigrams": _title_bigrams(title),
                    "awardee": awardee,
                    "result_date": _mie_wareki_iso(text),
                })
                import time as _time
                _time.sleep(0.4)
            import time as _time
            _time.sleep(0.5)
    logger.info(f"三重県 企画提案コンペ結果: {len(results)}件取得")
    return results


# ---------------------------------------------------------------------------
# 岐阜県（公式サイト「入札・公売」検索。ctg[]で種別を絞り込み、page=Nでページ送り）
# ---------------------------------------------------------------------------
_GIFU_BASE = "https://www.pref.gifu.lg.jp"
_GIFU_SEARCH = _GIFU_BASE + "/bid/search/search.php"
# ctg: 1=建設工事 2=物品 3=業務委託 4=その他 5=公募型プロポーザル（いずれも一般競争入札）
_GIFU_CATEGORIES = [
    ("1", "2", "3", "4"),  # 入札
    ("5",),                 # プロポーザル
]


def _gifu_date_iso(text: str) -> str:
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _scrape_gifu_sync(max_pages: int = 40, window_days: int = 90) -> List[Dict]:
    # 岐阜の検索は「更新日の新しい順に全履歴（約3000件・2年超）」を返すため、
    # 固定ページ数ではなく更新日ウィンドウで打ち切る。直近window_days日以内の
    # 公告（＝現在公告中＋直近終了分）のみ収集し、それより古い行が出たら停止する。
    from datetime import date, timedelta
    cutoff_iso = (date.today() - timedelta(days=window_days)).isoformat()
    import urllib.request
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    results = []
    seen = set()
    for ctg_values in _GIFU_CATEGORIES:
        cat = "プロポーザル" if ctg_values == ("5",) else "入札"
        qs_ctg = "&".join(f"ctg[]={v}" for v in ctg_values)
        stop = False
        for page in range(1, max_pages + 1):
            if stop:
                break
            url = f"{_GIFU_SEARCH}?{qs_ctg}&search=1&page={page}"
            try:
                html = get(url)
            except Exception as e:  # noqa: BLE001
                logger.error(f"岐阜県一覧取得失敗（ctg={ctg_values} page={page}）: {e}")
                break
            rows = re.findall(
                r'<span class="article_date">([^<]+)<span class="article_section">'
                r'(?:<a[^>]*>([^<]*)</a>)?</span></span>\s*'
                r'<span class="article_title"><a href="([^"]+)">([^<]+)</a>',
                html)
            if not rows:
                break
            new_count = 0
            for d, org, href, title in rows:
                pub = _gifu_date_iso(d)
                # 更新日が新しい順のため、ウィンドウ外に達したら以降は全て古い→停止
                if pub and pub < cutoff_iso:
                    stop = True
                    break
                full = href if href.startswith("http") else _GIFU_BASE + href
                if full in seen:
                    continue
                seen.add(full)
                new_count += 1
                title = title.strip()
                slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-")
                results.append({
                    "title":           title,
                    "category":        cat,
                    "organization":    ("岐阜県 " + (org or "").strip()).strip(),
                    "prefecture":      "岐阜県",
                    "published_at":    pub,
                    "deadline":        "",
                    "result_date":     "",
                    "result_url":      "",
                    "project_code":    f"GIFU-{slug}",
                    "awardee":         "",
                    "url":             full,
                    "source":          "GIFU",
                    "amount":          "",
                    "source_category": "",
                    "summary":         "",
                    "detail":          "",
                    "tags":            ",".join(generate_tags(title, org or "")),
                })
            if new_count == 0:
                break
            import time as _time
            _time.sleep(0.8)
    logger.info(f"岐阜県: {len(results)}件取得")
    return results


async def scrape_gifu() -> List[Dict]:
    """岐阜県公式サイト「入札・公売」検索（種別別ページ送り横断）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_gifu_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"岐阜県スクレイパー例外: {e}")
        return []


def fetch_gifu_detail(url: str) -> Optional[Dict]:
    """岐阜県 入札・公募 個別記事ページの本文を取得する。"""
    import urllib.request
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"岐阜県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find(id="main_body") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.pdf($|\?)", a["href"], re.I):
            name = re.sub(r"\s*\[[^\]]*\]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            full = a["href"] if a["href"].startswith("http") else _GIFU_BASE + a["href"]
            attachments.append({"name": name, "url": full, "kind": "公募要領"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments}


def fetch_gifu_award(url: str) -> Optional[Dict]:
    """岐阜県 案件の同一URLを再取得し、「選定結果」等のPDF添付があれば中身を解析して
    決定事業者を返す（岐阜県はタイトル・添付が同一URL上で更新されるため、
    結果もこの関数で取得する）。
    """
    import urllib.request
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"岐阜県結果取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find(id="main_body") or soup
    pdf_url = ""
    for a in main.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        if re.search(r"\.pdf($|\?)", a["href"], re.I) and re.search(r"選定結果|落札結果|入札結果", label):
            pdf_url = a["href"] if a["href"].startswith("http") else _GIFU_BASE + a["href"]
            break
    if not pdf_url:
        return None
    try:
        import io as _io
        from pypdf import PdfReader
        pdf_data = op.open(pdf_url, timeout=40).read()
        reader = PdfReader(_io.BytesIO(pdf_data))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:  # noqa: BLE001
        logger.error(f"岐阜県結果PDF解析失敗 {pdf_url}: {e}")
        return None
    m = re.search(r"(?:最優秀提案者（契約交渉の相手方）|最優秀提案者|落札者|契約の相手方)\s*\n\s*([^\n]+)", text)
    if not m:
        return None
    return {"awardee": m.group(1).strip()}


# ---------------------------------------------------------------------------
# 山梨県（「新着」一覧＋年度別アーカイブページ。工事以外の入札・プロポーザルを横断表示）
# ---------------------------------------------------------------------------
_YAMANASHI_BASE = "https://www.pref.yamanashi.jp"
_YAMANASHI_LIST = _YAMANASHI_BASE + "/shinchaku/kokoku/index.html"


def _yamanashi_reiwa_archive_url() -> str:
    """当該年度（4月始まり）の令和年数からアーカイブページURLを組み立てる。"""
    from datetime import date as _date
    today = _date.today()
    reiwa = today.year - 2018 if today.month >= 4 else today.year - 2019
    return f"{_YAMANASHI_BASE}/shinchaku/kokoku/r{reiwa}.html"


def _scrape_yamanashi_sync() -> List[Dict]:
    import urllib.request
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    results = []
    seen = set()
    for url in (_YAMANASHI_LIST, _yamanashi_reiwa_archive_url()):
        try:
            html = get(url)
        except Exception as e:  # noqa: BLE001
            logger.error(f"山梨県一覧取得失敗（{url}）: {e}")
            continue
        links = re.findall(r'<li><a href="([^"]+)">([^<]+)</a></li>', html)
        for href, title in links:
            full = href if href.startswith("http") else _YAMANASHI_BASE + href
            if full in seen:
                continue
            seen.add(full)
            title = title.strip()
            if title in _COMMON_SITE_NAV:  # サイト共通ナビ（案件でない）を除外
                continue
            # 年度別公告一覧へのハブリンク（例「令和7年度の公告（入札・公売等）」）は案件でない
            if re.match(r"^(平成|令和|Ｈ|H)\s*(元|\d+)\s*年度の公告", title):
                continue
            cat = "プロポーザル" if re.search(r"プロポーザル|企画提案|企画競争", title) else "入札"
            slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-")
            results.append({
                "title":           title,
                "category":        cat,
                "organization":    "山梨県",
                "prefecture":      "山梨県",
                "published_at":    "",  # 一覧に日付が無いため詳細ページの公示日で補完
                "deadline":        "",
                "result_date":     "",
                "result_url":      "",
                "project_code":    f"YAMANASHI-{slug}",
                "awardee":         "",
                "url":             full,
                "source":          "YAMANASHI",
                "amount":          "",
                "source_category": "",
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title)),
            })
    logger.info(f"山梨県: {len(results)}件取得")
    return results


async def scrape_yamanashi() -> List[Dict]:
    """山梨県公式サイト「公告（入札・公売等）」新着＋年度別一覧を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_yamanashi_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"山梨県スクレイパー例外: {e}")
        return []


def fetch_yamanashi_detail(url: str) -> Optional[Dict]:
    """山梨県 入札・公募 個別記事ページの本文を取得する（発注部署・公示日を構造化フィールドから抽出）。"""
    import urllib.request
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"山梨県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    # 公示日/更新日はヘッダ/メタ領域（本文スコープ外）のことがあるため decompose 前の全文から抽出
    published_at = _labeled_date_iso(soup.get_text(" ", strip=True), ["公示日", "更新日", "掲載日"])
    main = soup.find(id="tmp_contents") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.pdf($|\?)", a["href"], re.I):
            name = re.sub(r"[（(]\s*PDF[^）)]*[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            full = a["href"] if a["href"].startswith("http") else _YAMANASHI_BASE + a["href"]
            attachments.append({"name": name, "url": full, "kind": "公募要領"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments,
            "published_at": published_at}


# ---------------------------------------------------------------------------
# 富山県（種別ごとの静的一覧ページ。各1ページに全件表示・ページネーション無し）
# ---------------------------------------------------------------------------
_TOYAMA_BASE = "https://www.pref.toyama.jp"
_TOYAMA_SOURCES = [
    ("/sangyou/nyuusatsu/koubo/bosyuu.html", "プロポーザル"),
    ("/sangyou/nyuusatsu/jouhou/kouji/koukokukekka/koukoku.html", "入札"),
    ("/sangyou/nyuusatsu/jouhou/buppin/koukokukekka/koukoku.html", "入札"),
    ("/sangyou/nyuusatsu/jouhou/ekimu/koukokukekka/koukoku.html", "入札"),
    ("/sangyou/nyuusatsu/jouhou/sonota/koukokukekka/koukoku.html", "入札"),
]


# ページ本文に混在する県公式サイトのグローバルナビ（案件ではない）を除外する。
# 旧実装はページ内の <li><a> を無差別に取得し「リンク集」「庁舎案内」「入札公告（工事…）」等の
# ナビ項目を偽の案件として登録していた（富山32・山梨2の日付皆無ゴミ）。文言ブロックリストで除去。
_COMMON_SITE_NAV = {
    "庁舎案内", "リンク集", "サイトマップ", "よくある質問", "個人情報について",
    "ご意見・ご質問", "県ウェブサイトの考え方", "県ウェブサイトの使い方",
    "お知らせ", "ダウンロード", "動作環境について",
}
_TOYAMA_NAV_EXACT = _COMMON_SITE_NAV | {
    "募集", "結果", "その他", "電子入札について", "電子入札の流れ",
    "物品等電子入札質問フォーム", "オープンカウンター縦覧情報", "富山県物品等電子入札Webサイト",
}
_TOYAMA_NAV_RE = re.compile(
    r"^入札(発注見込み|結果|公告)（|電子入札.{0,4}(web|Web|ウェブ)?サイト|^富山県.{0,10}電子入札")


def _toyama_is_nav(title: str) -> bool:
    t = (title or "").strip()
    return t in _TOYAMA_NAV_EXACT or bool(_TOYAMA_NAV_RE.search(t))


def _toyama_pub_date_iso(title: str) -> str:
    """タイトル先頭の【令和X年M月D日公告】等から公告日を抽出する（複数付く場合は「公告」表記を優先）。"""
    matches = re.findall(r"令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日\s*(公告)?", title)
    if not matches:
        return ""
    tagged = [m for m in matches if m[3]]
    y, mo, d, _tag = tagged[0] if tagged else matches[0]
    return f"{2018 + int(y):04d}-{int(mo):02d}-{int(d):02d}"


def _scrape_toyama_sync() -> List[Dict]:
    import urllib.request
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    results = []
    seen = set()
    for path, cat in _TOYAMA_SOURCES:
        try:
            html = get(_TOYAMA_BASE + path)
        except Exception as e:  # noqa: BLE001
            logger.error(f"富山県一覧取得失敗（{path}）: {e}")
            continue
        links = re.findall(r'<li><a href="([^"]+)">([^<]+)</a></li>', html)
        for href, title in links:
            full = href if href.startswith("http") else _TOYAMA_BASE + href
            if full in seen:
                continue
            seen.add(full)
            title = title.strip()
            if _toyama_is_nav(title):  # ページ内ナビ（案件でない）を除外
                continue
            slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-")
            results.append({
                "title":           title,
                "category":        cat,
                "organization":    "富山県",
                "prefecture":      "富山県",
                "published_at":    _toyama_pub_date_iso(title),
                "deadline":        "",
                "result_date":     "",
                "result_url":      "",
                "project_code":    f"TOYAMA-{slug}",
                "awardee":         "",
                "url":             full,
                "source":          "TOYAMA",
                "amount":          "",
                "source_category": "",
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title)),
            })
        import time as _time
        _time.sleep(0.6)
    logger.info(f"富山県: {len(results)}件取得")
    return results


async def scrape_toyama() -> List[Dict]:
    """富山県公式サイト 公募型プロポーザル＋入札公告（種別別ページ）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_toyama_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"富山県スクレイパー例外: {e}")
        return []


def fetch_toyama_detail(url: str) -> Optional[Dict]:
    """富山県 入札・公募 個別記事ページの本文を取得する。"""
    import urllib.request
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"富山県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find(id="tmp_contents") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|zip|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            full = a["href"] if a["href"].startswith("http") else _TOYAMA_BASE + a["href"]
            attachments.append({"name": name, "url": full, "kind": "公募要領"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments}


def fetch_toyama_award(url: str) -> Optional[Dict]:
    """富山県 プロポーザル案件の同一URLを再取得し、契約候補者が決定していれば返す
    （富山県はタイトル・本文が同一URL上で更新されるため、結果もこの関数で取得する）。
    """
    import urllib.request
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"富山県結果取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find(id="tmp_contents") or soup
    text = main.get_text("\n", strip=True)
    m = re.search(r"契約候補者\n(.*?)(?:\n\d+\.|\Z)", text, re.S)
    if not m:
        return None
    block = m.group(1)
    names = re.findall(r"[（(]\d+[）)]\s*([^\n]+)", block)
    if not names:
        first_line = block.strip().split("\n")[0].strip()
        if first_line and "音順" not in first_line:
            names = [first_line]
    if not names:
        return None
    return {"awardee": "｜".join(names)}


# ---------------------------------------------------------------------------
# 長野県（公募型プロポーザル方式公告一覧。全庁横断・単一ページ・ページネーション無し）
# 一般競争入札の全庁横断一覧は無いため、プロポーザルのみ対応。
# ---------------------------------------------------------------------------
_NAGANO_LIST = "https://www.pref.nagano.lg.jp/kensa/puropo-kokoku.html"


def _nagano_date_iso(text: str) -> str:
    m = re.search(r"令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", text)
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{2018 + y:04d}-{mo:02d}-{d:02d}"


def _scrape_nagano_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    results = []
    try:
        html = get(_NAGANO_LIST)
    except Exception as e:  # noqa: BLE001
        logger.error(f"長野県一覧取得失敗: {e}")
        return results
    rows = re.findall(
        r'<td valign="middle">(令和[^<]+)</td>\s*'
        r'<td valign="middle"><a href="([^"]+)">([^<]+)</a></td>\s*'
        r'<td valign="middle">\s*(.*?)</td>',
        html, re.S)
    seen = set()
    for d, href, title, org_block in rows:
        full = urljoin(_NAGANO_LIST, href)
        if full in seen:
            continue
        seen.add(full)
        title = title.strip()
        org = " ".join(x.strip() for x in re.findall(r"<p[^>]*>([^<]*)</p>", org_block) if x.strip())
        slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-")
        results.append({
            "title":           title,
            "category":        "プロポーザル",
            "organization":    ("長野県 " + org).strip(),
            "prefecture":      "長野県",
            "published_at":    _nagano_date_iso(d),
            "deadline":        "",
            "result_date":     "",
            "result_url":      "",
            "project_code":    f"NAGANO-{slug}",
            "awardee":         "",
            "url":             full,
            "source":          "NAGANO",
            "amount":          "",
            "source_category": "",
            "summary":         "",
            "detail":          "",
            "tags":            ",".join(generate_tags(title, org)),
        })
    logger.info(f"長野県: {len(results)}件取得")
    return results


async def scrape_nagano() -> List[Dict]:
    """長野県公式サイト 公募型プロポーザル方式公告一覧（全庁横断）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_nagano_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"長野県スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 長野県 建設工事・測量コンサル等（長野県市町村電子調達システム SuperCALS）。
# 既存の長野スクレイパーは公募型プロポのみで入札が丸ごと欠落していたため補完する。
# ppi.e-nagano.lg.jp/ebidPPIPublish/EjPPIj（千葉と同型）。KikanNO=2000000（長野県。
# 市町村は2020100等）。一覧に状態列があり「落札/開札済」等が混在するため、現在公告中
# （公告掲載中/入札書受付中/開札執行前）だけに絞る。案件名は詳細ページから取得。
# ---------------------------------------------------------------------------
_NAGANO_CALS_EJ = "https://www.ppi.e-nagano.lg.jp/ebidPPIPublish/EjPPIj"
_NAGANO_CALS_KIKAN = "2000000"  # 長野県
_NAGANO_CALS_CHOUTATSU = [("00", "工事"), ("01", "測量・コンサル")]
_NAGANO_CALS_OPEN = ("公告掲載中", "入札書受付中", "開札執行前")
_NAGANO_CALS_WINDOW_DAYS = 90
_NAGANO_CALS_MAX_DETAIL = 200


def _scrape_nagano_cals_sync() -> List[Dict]:
    import hashlib
    import html as _html
    import time as _time
    import urllib.request
    import urllib.parse
    import http.cookiejar
    from datetime import date, timedelta

    today = date.today()
    lo = today - timedelta(days=_NAGANO_CALS_WINDOW_DAYS)
    bs, be = lo.strftime("%Y/%m/%d"), today.strftime("%Y/%m/%d")
    fy = today.year if today.month >= 4 else today.year - 1

    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", "Mozilla/5.0"), ("Referer", _NAGANO_CALS_EJ)]

    def post(pairs):
        body = urllib.parse.urlencode(pairs).encode()
        return op.open(_NAGANO_CALS_EJ, data=body, timeout=60).read().decode("cp932", "replace")

    def get(url):
        return op.open(url, timeout=60).read().decode("cp932", "replace")

    results: List[Dict] = []
    detail_budget = _NAGANO_CALS_MAX_DETAIL
    try:
        get(f"{_NAGANO_CALS_EJ}?KikanNO={_NAGANO_CALS_KIKAN}")
        post([("ejParameterID", "StartPage"), ("KikanNO", _NAGANO_CALS_KIKAN)])
    except Exception as e:  # noqa: BLE001
        logger.error(f"長野県建設セッション確立失敗: {e}")
        return results

    for cd, cd_label in _NAGANO_CALS_CHOUTATSU:
        try:
            post([("ejParameterID", "EjPSJ01"), ("ejProcessName", "start")])
            get(_NAGANO_CALS_EJ + "?ejParameterID=EjPSJ01&ejShousaiDispFlag=false&ejProcessName=getCondPage")
            lst = post([
                ("Nendo", str(fy)), ("KikanNO", _NAGANO_CALS_KIKAN), ("ChoutatsuCD", cd),
                ("BukyokuNO", ""), ("KoujiSyubetu", ""), ("BidStDate", bs), ("BidEnDate", be),
                ("kkselect", "AND"), ("mojisel1", ""), ("mojisel2", ""),
                ("chiiki_dataList", ""), ("chiikisentaku", ""), ("getStpos", "0"), ("AllhitSize", "0"),
                ("ejMaxDisplayRowCount", "700"), ("ejDisplaySort", "030006"), ("ejSortSequence", "desc"),
                ("ejParameterID", "EjPSJ01"), ("ejProcessName", "findList"), ("ejShousaiDispFlag", "false"),
            ])
        except Exception as e:  # noqa: BLE001
            logger.error(f"長野県建設検索失敗（{cd_label}）: {e}")
            continue

        fv = re.search(r'ejFindVersion"\s*value="(\d+)"', lst)
        find_version = fv.group(1) if fv else ""
        if not find_version:
            logger.info(f"長野県建設（{cd_label}）: 該当なしまたはejFindVersion取得失敗")
            continue

        for tr in re.findall(r"<TR[^>]*>(.*?)</TR>", lst, re.S | re.I):
            if "openYotei" not in tr:
                continue
            idxm = re.search(r"openYotei\('?(\d+)'?\)", tr)
            if not idxm:
                continue
            idx = idxm.group(1)
            tds = re.findall(r"<TD[^>]*>(.*?)</TD>", tr, re.S)
            cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", c))).replace("\xa0", " ").strip() for c in tds]
            status = cells[4] if len(cells) > 4 else ""
            # 現在公告中（開札前）のみ対象。落札・開札済・中止・不調は除外
            if not any(status.startswith(s) for s in _NAGANO_CALS_OPEN):
                continue
            list_title = cells[3] if len(cells) > 3 else ""

            if detail_budget <= 0:
                continue
            try:
                dv = post([
                    ("ejParameterID", "EjPSJ01"), ("ejProcessName", "getDetailPage"),
                    ("ejCategoryName", "display"), ("ejKeyNo", idx), ("ejFindVersion", find_version),
                    ("ejStartPosition", "0"), ("ejMaxDisplayRowCount", "700"), ("ejShousaiDispFlag", "false"),
                ])
                detail_budget -= 1
                info = _parse_chiba_cals_detail(dv)
                _time.sleep(0.2)
            except Exception as e:  # noqa: BLE001
                logger.error(f"長野県建設詳細取得失敗（idx={idx}）: {e}")
                continue

            title = info["title"] or re.split(r"\s{2,}", list_title)[0].strip()
            if not title:
                continue
            published = info["published_at"]
            slug = hashlib.md5((title + (published or idx)).encode("utf-8")).hexdigest()[:12]
            results.append({
                "title":           title,
                "category":        "入札",
                "organization":    info["org"] or "長野県",
                "prefecture":      "長野県",
                "published_at":    published,
                "deadline":        info["deadline"],
                "result_date":     "",
                "result_url":      "",
                "project_code":    f"NAGANO-CALS-{slug}",
                "awardee":         "",
                "url":             f"{_NAGANO_CALS_EJ}?KikanNO={_NAGANO_CALS_KIKAN}#{slug}",
                "source":          "NAGANO",
                "amount":          info["amount"],
                "source_category": info["gyoshu"] or cd_label,
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title, info["org"] or "長野県")),
            })
    logger.info(f"長野県 建設工事・測量(電子入札): {len(results)}件取得")
    return results


async def scrape_nagano_cals() -> List[Dict]:
    """長野県 建設工事・測量コンサル等（長野県市町村電子調達システム）の現在公告中を取得する。

    一覧の状態列で現在公告中（公告掲載中/入札書受付中/開札執行前）に絞り、案件名・公告日・
    入札書受付締切・業種・予定価格は詳細ページから確定させる（セッション依存）。
    """
    try:
        return await asyncio.to_thread(_scrape_nagano_cals_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"長野県建設スクレイパー例外: {e}")
        return []


def fetch_nagano_detail(url: str) -> Optional[Dict]:
    """長野県 入札・公募 個別記事ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    # 電子入札システム(SuperCALS)の案件はセッション依存で詳細確定済み。スキップ。
    if "ebidPPIPublish" in url:
        return None
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"長野県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find(id="tmp_readcontents") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|zip|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            # 詳細ページが県立学校等の別ドメインにある場合もあるため、実ページURL基準で結合する
            full = urljoin(url, a["href"])
            attachments.append({"name": name, "url": full, "kind": "公募要領"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments}


# ---------------------------------------------------------------------------
# 栃木県（「入札・公募」カテゴリ別一覧。業務委託/公共事業/物品/その他の4ページを横断。
# 一覧は<li><a>のみで日付なし→掲載日は詳細ページの「更新日」で補完。newest-first順の
# ため初回バックフィルは各カテゴリ先頭MAXだけに絞る。robots.txt禁止パス(/koujisoutatsu/)
# には該当しない。）
# ---------------------------------------------------------------------------
_TOCHIGI_BASE = "https://www.pref.tochigi.lg.jp"
_TOCHIGI_CATEGORIES = [
    ("/kensei/nyuusatsu/koubo-itaku/index.html", "入札・公募（業務委託）"),
    ("/kensei/nyuusatsu/koubo-koukyou/index.html", "入札・公募（公共事業）"),
    ("/kensei/nyuusatsu/koubo-buppin/index.html", "入札・公募（物品調達）"),
    ("/kensei/nyuusatsu/koubo-sonota/index.html", "入札・公募（その他）"),
]
_TOCHIGI_MAX_PER_CAT = 40  # 各カテゴリ先頭40件（浅めの初回バックフィル）


def _scrape_tochigi_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    results = []
    seen = set()
    for path, cat_label in _TOCHIGI_CATEGORIES:
        try:
            html = get(_TOCHIGI_BASE + path)
        except Exception as e:  # noqa: BLE001
            logger.error(f"栃木県一覧取得失敗（{path}）: {e}")
            continue
        m = re.search(r"<h1>[^<]*</h1>\s*(.*?)(?:</div>|<h2)", html, re.S)
        body = m.group(1) if m else html
        items = re.findall(r'<li><a href="([^"]+)">([^<]+)</a></li>', body)
        for href, title in items[:_TOCHIGI_MAX_PER_CAT]:
            full = urljoin(_TOCHIGI_BASE + path, href)
            if full in seen:
                continue
            seen.add(full)
            title = title.strip()
            cat = "プロポーザル" if re.search(r"プロポーザル|企画提案|企画競争", title) else "入札"
            slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-")
            results.append({
                "title":           title,
                "category":        cat,
                "organization":    "栃木県",
                "prefecture":      "栃木県",
                "published_at":    "",  # 一覧に日付なし→詳細ページの更新日で補完
                "deadline":        "",
                "result_date":     "",
                "result_url":      "",
                "project_code":    f"TOCHIGI-{slug}",
                "awardee":         "",
                "url":             full,
                "source":          "TOCHIGI",
                "amount":          "",
                "source_category": cat_label,
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title)),
            })
        import time as _time
        _time.sleep(0.6)
    logger.info(f"栃木県: {len(results)}件取得")
    return results


async def scrape_tochigi() -> List[Dict]:
    """栃木県公式サイト 入札・公募（4カテゴリ）一覧を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_tochigi_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"栃木県スクレイパー例外: {e}")
        return []


def fetch_tochigi_detail(url: str) -> Optional[Dict]:
    """栃木県 入札・公募 個別記事ページの本文を取得する（更新日を掲載日として補完）。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"栃木県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find(id="tmp_contents") or soup.find(id="tmp_read_contents") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    published_at = ""
    m = re.search(r"更新日[：:\s]*(\d{4})年(\d{1,2})月(\d{1,2})日", soup.get_text(" ", strip=True))
    if m:
        published_at = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            full = urljoin(url, a["href"])
            attachments.append({"name": name, "url": full, "kind": "公募要領"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments,
            "published_at": published_at}


# ---------------------------------------------------------------------------
# 千葉県（「入札等の公告(物品・委託等)」→「現在公告中の案件」。企画提案(プロポーザル)
# が中心の静的<li><a>一覧。現在公告中のみ＝件数が少なく初回から浅い。掲載日は詳細
# ページの「更新日：令和X(YYYY)年M月D日」（和暦）から補完。建設工事は別系統で対象外。
# ---------------------------------------------------------------------------
_CHIBA_BASE = "https://www.pref.chiba.lg.jp"
_CHIBA_LIST = _CHIBA_BASE + "/nyuu-kei/buppin-itaku/nyuusatsukoukoku/koukoku/index.html"


def _chiba_wareki_iso(text: str) -> str:
    # 「令和8(2026)年6月29日」「令和8年6月29日」両形式に対応
    m = re.search(r"令和\s*(\d+)\s*(?:\(\d+\))?\s*年\s*(\d+)\s*月\s*(\d+)\s*日", text)
    if m:
        return f"{2018 + int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{4})年\s*(\d+)\s*月\s*(\d+)\s*日", text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def _scrape_chiba_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    results = []
    try:
        html = get(_CHIBA_LIST)
    except Exception as e:  # noqa: BLE001
        logger.error(f"千葉県一覧取得失敗: {e}")
        return results
    m = re.search(r"<h1[^>]*>現在公告中の案件</h1>(.*?)(?:<h2|</main|footer)", html, re.S)
    body = m.group(1) if m else html
    items = re.findall(r'<li>\s*<a href="\s*([^"]+?)\s*">([^<]+)</a>', body)
    seen = set()
    for href, title in items:
        full = urljoin(_CHIBA_LIST, href.strip())
        if full in seen:
            continue
        seen.add(full)
        title = title.strip()
        cat = "プロポーザル" if re.search(r"プロポーザル|企画提案|公募", title) else "入札"
        slug = re.sub(r"[^A-Za-z0-9]+", "-", href.strip().rsplit("/", 1)[-1]).strip("-")
        results.append({
            "title":           title,
            "category":        cat,
            "organization":    "千葉県",
            "prefecture":      "千葉県",
            "published_at":    "",  # 一覧に日付なし→詳細ページの更新日で補完
            "deadline":        "",
            "result_date":     "",
            "result_url":      "",
            "project_code":    f"CHIBA-{slug}",
            "awardee":         "",
            "url":             full,
            "source":          "CHIBA",
            "amount":          "",
            "source_category": "",
            "summary":         "",
            "detail":          "",
            "tags":            ",".join(generate_tags(title)),
        })
    logger.info(f"千葉県: {len(results)}件取得")
    return results


async def scrape_chiba() -> List[Dict]:
    """千葉県公式サイト「現在公告中の案件」（物品・委託等の企画提案）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_chiba_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"千葉県スクレイパー例外: {e}")
        return []


def fetch_chiba_detail(url: str) -> Optional[Dict]:
    """千葉県 入札・公募 個別記事ページの本文を取得する（更新日を掲載日として補完）。"""
    import urllib.request
    from urllib.parse import urljoin
    # 電子調達システム(SuperCALS)の案件はセッション依存でスクレイパー側が
    # 詳細まで確定済み。ポータルURLをここで叩いても意味がないためスキップ。
    if "ebidPPIPublish" in url:
        return None
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"千葉県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find(id="tmp_contents") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    published_at = ""
    m = re.search(r"更新日[：:\s]*([^\n<]{6,25})", soup.get_text(" ", strip=True))
    if m:
        published_at = _chiba_wareki_iso(m.group(1))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            full = urljoin(url, a["href"])
            attachments.append({"name": name, "url": full, "kind": "公募要領"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments,
            "published_at": published_at}


# ---------------------------------------------------------------------------
# 千葉県 建設工事・測量（ちば電子調達システム SuperCALS「入札情報サービス」）。
# 既存の千葉スクレイパー（物品・委託の静的ページ）は建設工事を含まないため、
# 電子調達システムの入札予定(公告)をセッションPOST連鎖で取得して補完する。
# フロー: GET(cookie)→StartPage→EjPSJ01/start→getCondPage→findList→getDetailPage。
# ChoutatsuCD=00(工事)/01(測量)。KikanNO=1200000(千葉県本体)。応答はShift_JIS。
# 一覧・詳細ともセッション依存のため、石川と同様にこの関数内で全項目を確定させる。
# ---------------------------------------------------------------------------
_CHIBA_CALS_EJ = "https://www.chiba-ep-bis.supercals.jp/ebidPPIPublish/EjPPIj"
_CHIBA_CALS_KIKAN = "1200000"  # 千葉県本体（departArray[0]）
_CHIBA_CALS_CHOUTATSU = [("00", "工事"), ("01", "測量")]
_CHIBA_CALS_MAX_DETAIL = 400   # 1run当たり詳細取得の上限（暴走・過負荷防止）
_CHIBA_CALS_ROW_RE = re.compile(r"<TR[^>]*>(.*?)</TR>", re.S | re.I)


def _chiba_cals_wareki(text: str) -> str:
    # 「令和08-06-19」「R08-07-13」→ ISO（令和のみ。R=令和）
    m = re.search(r"(?:令和|R)\s*(\d{1,2})[-年.](\d{1,2})[-月.](\d{1,2})", text)
    if not m:
        return ""
    return f"{2018 + int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _parse_chiba_cals_detail(html_text: str) -> Dict:
    import html as _html
    # 詳細ページの<td>ラベル→次セル値を対応づける。
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", re.sub(r"<script.*?</script>", "", html_text, flags=re.S | re.I), re.S | re.I)
    cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", c))).replace("\xa0", " ").strip() for c in cells]
    cells = [c for c in cells if c]
    # SuperCALS PPIの詳細ラベルは県で微妙に異なる（千葉/福井で共用するため両対応）:
    #   締切: 千葉「入札締切予定日時」/ 福井「入札書受付終了予定日」
    #   発注: 千葉「入札担当部署」/ 福井「発注機関」等
    #   工種: 千葉「工種又は業種」/ 福井「工事種別」
    info = {"org": "", "published_at": "", "deadline": "", "amount": "", "gyoshu": "", "title": ""}
    for i, c in enumerate(cells):
        nxt = cells[i + 1] if i + 1 < len(cells) else ""
        # 案件名（詳細から取る県用。千葉/福井/石川は一覧から取るので上書きしない）
        if (c in ("案件名称", "工事名称", "業務名称", "調達案件名称", "案件名") or c.endswith("案件名称")) and not info["title"]:
            info["title"] = re.sub(r"\s+", " ", nxt).strip()
        elif c in ("入札担当部署", "発注機関", "発注部署", "発注課") and not info["org"]:
            info["org"] = re.sub(r"\s+", " ", nxt).strip()
        elif "公告日" in c and not info["published_at"]:  # 公告日／公告日又は指名通知日 等
            info["published_at"] = _chiba_cals_wareki(nxt)
        elif ("入札締切" in c or "入札書受付" in c or "入札受付締切" in c
              or "開札予定日" in c) and not info["deadline"]:
            # 締切(入札書受付予定/終了)優先。無ければ開札予定日時をフォールバック。
            info["deadline"] = _chiba_cals_wareki(nxt)
        elif c.startswith("予定価格") and not info["amount"]:
            info["amount"] = nxt.strip()
        elif ("工種" in c or "業種" in c or "工事種別" in c) and not info["gyoshu"]:
            info["gyoshu"] = nxt.strip()
    return info


def _scrape_chiba_cals_sync() -> List[Dict]:
    import hashlib
    import html as _html
    import time as _time
    import urllib.request
    import urllib.parse
    import http.cookiejar
    from datetime import date, timedelta

    # 入札予定日が古い（終了）案件を落とす窓。PPIの入札予定一覧は概ね現行分のみ
    # だが念のため直近30日より前は除外する。
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    today = date.today()
    fy = today.year if today.month >= 4 else today.year - 1

    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def post(data):
        body = urllib.parse.urlencode(data).encode()
        return op.open(_CHIBA_CALS_EJ, data=body, timeout=60).read().decode("cp932", "replace")

    def get(url):
        return op.open(url, timeout=60).read().decode("cp932", "replace")

    results: List[Dict] = []
    detail_budget = _CHIBA_CALS_MAX_DETAIL
    try:
        get(_CHIBA_CALS_EJ)              # cookie確立
        post({"ejParameterID": "StartPage"})
    except Exception as e:  # noqa: BLE001
        logger.error(f"千葉県電子調達セッション確立失敗: {e}")
        return results

    for cd, cd_label in _CHIBA_CALS_CHOUTATSU:
        try:
            post({"ejParameterID": "EjPSJ01", "ejProcessName": "start"})
            get(_CHIBA_CALS_EJ + "?ejParameterID=EjPSJ01&ejShousaiDispFlag=false&ejProcessName=getCondPage")
            lst = post({
                "ejParameterID": "EjPSJ01", "ejProcessName": "findList", "ejShousaiDispFlag": "false",
                "Nendo": str(fy), "KikanNO": _CHIBA_CALS_KIKAN, "ChoutatsuCD": cd,
                "BukyokuNO": "", "KoujiSyubetu": "", "BidStDate": "", "BidEnDate": "",
                "kkselect": "AND", "mojisel1": "", "mojisel2": "",
                "chiiki_dataList": "", "chiikisentaku": "", "getStpos": "0", "AllhitSize": "0",
                "ejMaxDisplayRowCount": "500", "ejDisplaySort": "030006", "ejSortSequence": "desc",
            })
        except Exception as e:  # noqa: BLE001
            logger.error(f"千葉県電子調達検索失敗（{cd_label}）: {e}")
            continue

        fv = re.search(r'ejFindVersion"\s*value="(\d+)"', lst)
        find_version = fv.group(1) if fv else ""
        if not find_version:
            logger.info(f"千葉県電子調達（{cd_label}）: ejFindVersion取得失敗")
            continue

        for tr in _CHIBA_CALS_ROW_RE.findall(lst):
            if "openYotei" not in tr:
                continue
            idxm = re.search(r"openYotei\('(\d+)'\)", tr)
            if not idxm:
                continue
            idx = idxm.group(1)
            tds = re.findall(r"<TD[^>]*>(.*?)</TD>", tr, re.S)
            cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", c))).replace("\xa0", " ").strip() for c in tds]
            yotei = _chiba_cals_wareki(cells[1]) if len(cells) > 1 else ""
            title = re.sub(r"\s*※\s*添付有\s*$", "", cells[2]).strip() if len(cells) > 2 else ""
            price = cells[6] if len(cells) > 6 else ""
            if not title:
                continue
            if yotei and yotei < cutoff:
                continue

            org, published, deadline, amount, gyoshu = "千葉県", "", "", price, cd_label
            if detail_budget > 0:
                try:
                    dv = post({
                        "ejParameterID": "EjPSJ01", "ejProcessName": "getDetailPage",
                        "ejCategoryName": "display", "ejKeyNo": idx, "ejFindVersion": find_version,
                        "ejStartPosition": "0", "ejMaxDisplayRowCount": "500", "ejShousaiDispFlag": "false",
                    })
                    detail_budget -= 1
                    info = _parse_chiba_cals_detail(dv)
                    org = info["org"] or org
                    published = info["published_at"]
                    deadline = info["deadline"]
                    amount = info["amount"] or price
                    gyoshu = info["gyoshu"] or cd_label
                    _time.sleep(0.25)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"千葉県電子調達詳細取得失敗（{title}）: {e}")

            # slugは実行間で安定な値のみ（idx=位置番号は毎回変わりURLがぶれて重複蓄積
            # するため使わない）。案件名＋公告/予定日で一意化（CSVから復元可能な値）。
            slug = hashlib.md5((title + yotei).encode("utf-8")).hexdigest()[:12]
            results.append({
                "title":           title,
                "category":        "入札",
                "organization":    org,
                "prefecture":      "千葉県",
                "published_at":    published,
                "deadline":        deadline,
                "result_date":     "",
                "result_url":      "",
                "project_code":    f"CHIBA-CALS-{slug}",
                "awardee":         "",
                "url":             f"{_CHIBA_CALS_EJ}?KikanNO={_CHIBA_CALS_KIKAN}#{slug}",
                "source":          "CHIBA",
                "amount":          amount,
                "source_category": gyoshu,
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title, org)),
            })
    logger.info(f"千葉県 建設工事・測量(電子調達): {len(results)}件取得")
    return results


async def scrape_chiba_cals() -> List[Dict]:
    """千葉県 建設工事・測量（ちば電子調達システム）の入札予定(公告)を取得する。

    一覧・詳細ともセッション依存のため、この関数内でorganization・公告日・締切まで
    確定させる（後段の詳細取得フェーズは通さない。fetch_chiba_detailはsupercals
    URLをスキップする）。
    """
    try:
        return await asyncio.to_thread(_scrape_chiba_cals_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"千葉県電子調達スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 京都府（「入札・プロポーザル情報」新着一覧。<table class="list_table">が2つ
# （入札情報／プロポーザル情報）。各行が「M月D日公告」＋案件名リンク。日付は年が
# 無いため当月基準で補完（当月以前=今年、それ以降=前年の年度繰り越し）。詳細ページに
# 更新日(西暦)もあり補完に使える。建設工事系は含まれるが除外はしない（委託・物品中心）。
# ---------------------------------------------------------------------------
_KYOTO_BASE = "https://www.pref.kyoto.jp"
_KYOTO_LIST = _KYOTO_BASE + "/shinchaku/nyusatsu/index.html"


def _kyoto_date_iso(md: str) -> str:
    # 「7月1日公告」→ 年を補完してISO。当月以前は今年、先の月は前年度扱い。
    import datetime as _dt
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", md)
    if not m:
        return ""
    mo, d = int(m.group(1)), int(m.group(2))
    today = _dt.date.today()
    year = today.year if mo <= today.month else today.year - 1
    try:
        return f"{year:04d}-{mo:02d}-{d:02d}"
    except Exception:  # noqa: BLE001
        return ""


def _scrape_kyoto_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    results = []
    try:
        html = get(_KYOTO_LIST)
    except Exception as e:  # noqa: BLE001
        logger.error(f"京都府一覧取得失敗: {e}")
        return results
    # 2つの list_table を、直前のh2見出しでカテゴリ判定
    seen = set()
    for sec in re.finditer(r'<h2>.*?>([^<]+)</h2>(.*?)</table>', html, re.S):
        head = sec.group(1)
        cat = "プロポーザル" if "プロポーザル" in head else "入札"
        rows = re.findall(
            r'<td class="date_year"><p>([^<]+)</p></td>\s*'
            r'<td><p><a href="([^"]+)">([^<]+)</a>', sec.group(2), re.S)
        for md, href, title in rows:
            full = urljoin(_KYOTO_LIST, href.strip())
            if full in seen:
                continue
            seen.add(full)
            title = title.strip()
            row_cat = "プロポーザル" if re.search(r"プロポーザル|企画提案|企画競争", title) else cat
            slug = re.sub(r"[^A-Za-z0-9]+", "-", href.strip().rsplit("/", 1)[-1]).strip("-")
            results.append({
                "title":           title,
                "category":        row_cat,
                "organization":    "京都府",
                "prefecture":      "京都府",
                "published_at":    _kyoto_date_iso(md),
                "deadline":        "",
                "result_date":     "",
                "result_url":      "",
                "project_code":    f"KYOTO-{slug}",
                "awardee":         "",
                "url":             full,
                "source":          "KYOTO",
                "amount":          "",
                "source_category": "",
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title)),
            })
    logger.info(f"京都府: {len(results)}件取得")
    return results


async def scrape_kyoto() -> List[Dict]:
    """京都府公式サイト「入札・プロポーザル情報」新着一覧を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_kyoto_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"京都府スクレイパー例外: {e}")
        return []


def fetch_kyoto_detail(url: str) -> Optional[Dict]:
    """京都府 入札・公募 個別記事ページの本文を取得する（更新日を掲載日の補完に使う）。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"京都府詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    main = (soup.find(id=re.compile(r"contents|honbun|main", re.I))
            or soup.find("main") or soup)
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    published_at = ""
    m = re.search(r"(?:更新日|掲載日)[：:\s]*(\d{4})年(\d{1,2})月(\d{1,2})日", soup.get_text(" ", strip=True))
    if m:
        published_at = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            full = urljoin(url, a["href"])
            attachments.append({"name": name, "url": full, "kind": "公募要領"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments,
            "published_at": published_at}


# ---------------------------------------------------------------------------
# 兵庫県（新規）。県公式の静的な入札公告ページ（カテゴリ別）。1案件=1テーブルで
# 名称/種別/発注機関/入札方法/入札予定日/公示日/申込期限日 が縦に並ぶ。日付は西暦。
# 委託・役務／工事・設計／その他 の3カテゴリを巡回。CALS不要でクリーンに取れる。
# ---------------------------------------------------------------------------
_HYOGO_BASE = "https://web.pref.hyogo.lg.jp"
_HYOGO_CATEGORIES = [
    ("/bid/bid_opn_01.html", "物品"),
    ("/bid/bid_opn_02.html", "委託・役務"),
    ("/bid/bid_opn_03.html", "工事・設計"),
    ("/bid/bid_opn_04.html", "その他"),
]


def _hyogo_date_iso(text: str) -> str:
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if not m:
        return ""
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _scrape_hyogo_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    results, seen = [], set()
    for path, cat_label in _HYOGO_CATEGORIES:
        try:
            html_doc = get(_HYOGO_BASE + path)
        except Exception as e:  # noqa: BLE001
            logger.error(f"兵庫県一覧取得失敗（{cat_label}）: {e}")
            continue
        for tb in re.findall(r"<table[^>]*>(.*?)</table>", html_doc, re.S | re.I):
            if "名称" not in tb or not re.search(r'href="/[a-z]', tb):
                continue
            # 縦型テーブル: 各行 <th/td>ラベル</><td>値</>
            fields = {}
            link = ""
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tb, re.S | re.I):
                cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
                if len(cells) < 2:
                    continue
                label = re.sub(r"<[^>]+>", "", cells[0]).strip()
                val = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", cells[1])).strip()
                fields[label] = val
                if label == "名称":
                    lm = re.search(r'href="([^"]+)"', cells[1])
                    if lm:
                        link = lm.group(1)
            title = fields.get("名称", "").strip()
            if not title or not link:
                continue
            url = urljoin(_HYOGO_BASE, link)
            if url in seen:
                continue
            seen.add(url)
            method = fields.get("入札方法", "")
            cat = "プロポーザル" if re.search(r"プロポーザル|企画提案|公募型", method + title) else "入札"
            org = ("兵庫県 " + fields.get("発注機関", "")).strip()
            slug = re.sub(r"[^A-Za-z0-9]+", "-", link.rsplit("/", 1)[-1]).strip("-") or str(len(seen))
            results.append({
                "title":           title,
                "category":        cat,
                "organization":    org,
                "prefecture":      "兵庫県",
                "published_at":    _hyogo_date_iso(fields.get("公示日", "")),
                "deadline":        _hyogo_date_iso(fields.get("申込期限日", "")),
                "result_date":     "",
                "result_url":      "",
                "project_code":    f"HYOGO-{slug}",
                "awardee":         "",
                "url":             url,
                "source":          "HYOGO",
                "amount":          "",
                "source_category": fields.get("種別", cat_label),
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title, fields.get("発注機関", ""))),
            })
    logger.info(f"兵庫県: {len(results)}件取得")
    return results


async def scrape_hyogo() -> List[Dict]:
    """兵庫県公式サイトの入札公告（委託・役務／工事・設計／その他）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_hyogo_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"兵庫県スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 滋賀県（新規）。県公式の静的「公告一覧（物品・委託・役務）」。各項目が
# <li class="display_date"><time datetime="YYYY-MM-DD">…<a href="XX.html">○○の公告（案件名）</a>。
# 日付(公告日)が一覧に入っている。締切は詳細ページ側。琵琶湖・生物多様性等が多く海洋系に好適。
# ---------------------------------------------------------------------------
_SHIGA_BASE = "https://www.pref.shiga.lg.jp/zigyousya/nyusatsubaikyaku/"
# (カテゴリ, 表示名, strict)。itakuは委託の実案件一覧なので全件。他カテゴリは
# 常設ページ(発注見通し/参加停止/制度/資格/様式等)が大半なので、実案件の公告のみ厳選。
_SHIGA_CATEGORIES = [
    ("itaku", "委託・役務", False),
    ("kouzi", "工事", True),
    ("nyusatsu", "物品・入札", True),
    ("keiyaku", "契約", True),
    ("baikyaku", "売却・貸付", True),
    ("shinrin", "森林", True),
]
# strictカテゴリで採用する語（実案件の公告・公募）と、除外する常設ページ語。
_SHIGA_INCLUDE = re.compile(r"公告|公募|プロポーザル|企画提案|企画競争|参加者募集|告示第\d+号")
_SHIGA_EXCLUDE = re.compile(
    r"発注見通し|見通し|参加停止|について$|について（|制度|要綱|様式|マニュアル|ガイド|"
    r"規程|規則|ＦＡＱ|FAQ|回答|参加資格|変更届|ポータル|システム|基準|条例|委員会|"
    r"よくある|申請書|苦情|措置|融資|情報$|一覧$")
_SHIGA_ROW = re.compile(
    r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})[^"]*"[^>]*>.*?'
    r'<a href="([^"]+)">([^<]+)</a>', re.S)


def _scrape_shiga_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    results, seen = [], set()
    for cat_key, cat_label, strict in _SHIGA_CATEGORIES:
        list_url = _SHIGA_BASE + cat_key + "/"
        try:
            html_doc = op.open(list_url, timeout=40).read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            logger.error(f"滋賀県一覧取得失敗（{cat_label}）: {e}")
            continue
        for m in _SHIGA_ROW.finditer(html_doc):
            date_iso, href, raw_title = m.group(1), m.group(2), m.group(3).strip()
            if ".htm" not in href:
                continue
            if strict and (not _SHIGA_INCLUDE.search(raw_title) or _SHIGA_EXCLUDE.search(raw_title)):
                continue
            url = urljoin(list_url, href)
            if url in seen:
                continue
            seen.add(url)
            # 「公募型プロポーザルの公告（案件名）」→ 案件名を取り出す
            mt = re.search(r"公告(?:（|\()(.+?)(?:）|\))\s*$", raw_title)
            title = (mt.group(1) if mt else raw_title).strip()
            if len(title) < 4:
                continue
            is_result = bool(re.search(r"結果について|落札|選定結果", raw_title))
            cat = "プロポーザル" if "プロポーザル" in raw_title or "企画提案" in raw_title else "入札"
            slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-") or str(len(seen))
            results.append({
                "title":           title,
                "category":        cat,
                "organization":    "滋賀県",
                "prefecture":      "滋賀県",
                "published_at":    "" if is_result else date_iso,
                "deadline":        "",
                "result_date":     date_iso if is_result else "",
                "result_url":      url if is_result else "",
                "project_code":    f"SHIGA-{'R-' if is_result else ''}{slug}",
                "awardee":         "",
                "awardee_checked": "1" if is_result else "",
                "url":             url,
                "source":          "SHIGA",
                "amount":          "",
                "source_category": cat_label,
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title)),
            })
    logger.info(f"滋賀県: {len(results)}件取得")
    return results


async def scrape_shiga() -> List[Dict]:
    """滋賀県公式サイト「公告一覧（物品・委託・役務）」を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_shiga_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"滋賀県スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 和歌山県（新規）。県公式「入札・物品・役務」新着一覧が静的テーブル：
# <tr><td>令和8年7月13日</td><td><a href="/prefg/../d00XXXXX.html">案件名</a></td><td>課名</td></tr>
# 公告・プロポ・入札結果が混在。【入札結果の掲載】等は結果レコードとして扱う。
# ---------------------------------------------------------------------------
_WAKAYAMA_BASE = "https://www.pref.wakayama.lg.jp"
_WAKAYAMA_LIST = _WAKAYAMA_BASE + "/whatsnew/nyusatsu.html"
_WAKAYAMA_ROW = re.compile(
    r"<tr>\s*<td>\s*(令和\d+年\d+月\d+日)\s*</td>\s*"
    r'<td>\s*<a href="([^"]+)">([^<]+)</a>\s*</td>\s*'
    r"<td>([^<]*)</td>", re.S)


def _wakayama_date_iso(text: str) -> str:
    m = re.search(r"令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", text)
    if not m:
        return ""
    return f"{2018 + int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _scrape_wakayama_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    try:
        html_doc = op.open(_WAKAYAMA_LIST, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"和歌山県一覧取得失敗: {e}")
        return []
    results, seen = [], set()
    for m in _WAKAYAMA_ROW.finditer(html_doc):
        date_iso = _wakayama_date_iso(m.group(1))
        href, raw_title, org = m.group(2), m.group(3).strip(), m.group(4).strip()
        if not href.endswith(".html"):
            continue
        url = urljoin(_WAKAYAMA_LIST, href)
        if url in seen:
            continue
        seen.add(url)
        is_result = bool(re.search(r"入札結果|落札|開札結果|結果の掲載|選定結果", raw_title))
        # 先頭の【…】マーカーを除去
        title = re.sub(r"^【[^】]*】\s*", "", raw_title).strip()
        if len(title) < 4:
            continue
        cat = "プロポーザル" if re.search(r"プロポーザル|企画提案|コンペ", raw_title) else "入札"
        slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-") or str(len(seen))
        results.append({
            "title":           title,
            "category":        cat,
            "organization":    ("和歌山県 " + org).strip(),
            "prefecture":      "和歌山県",
            "published_at":    "" if is_result else date_iso,
            "deadline":        "",
            "result_date":     date_iso if is_result else "",
            "result_url":      url if is_result else "",
            "project_code":    f"WAKAYAMA-{'R-' if is_result else ''}{slug}",
            "awardee":         "",
            "awardee_checked": "1" if is_result else "",
            "amount":          "",
            "url":             url,
            "source":          "WAKAYAMA",
            "source_category": "入札結果" if is_result else "",
            "summary":         "",
            "detail":          "",
            "tags":            ",".join(generate_tags(title, org)),
        })
    logger.info(f"和歌山県: {len(results)}件取得")
    return results


async def scrape_wakayama() -> List[Dict]:
    """和歌山県公式サイト「入札・物品・役務」新着一覧を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_wakayama_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"和歌山県スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 広島県（新規）。県公式の静的入札公告一覧（カテゴリ別）。各項目が
# <li><span class=article_title><a href="X.html">案件名</a></span><span class=article_date>2026年7月17日</span>。
# 物品／その他委託役務／電子入札案件公告 の3カテゴリを巡回。日付は西暦。
# ---------------------------------------------------------------------------
_HIROSHIMA_BASE = "https://www.pref.hiroshima.lg.jp"
# (path, 表示名, is_result)。ハブ(nyusatsukeiyaku)配下の list945-XXXX が各カテゴリ一覧。
# 4046=企画提案(プロポーザル)を取っていなかった＝広島のプロポ完全欠落を修正。
_HIROSHIMA_CATEGORIES = [
    ("/site/nyusatsukeiyaku/list945-4044.html", "物品", False),
    ("/site/nyusatsukeiyaku/list945-4041.html", "委託・役務", False),
    ("/site/nyusatsukeiyaku/list945-4042.html", "庁舎・設備管理", False),
    ("/site/nyusatsukeiyaku/list945-4046.html", "企画提案（プロポーザル）", False),
    ("/site/nyusatsukeiyaku/list945-5244.html", "電子入札公告", False),
    ("/site/nyusatsukeiyaku/list945-13098.html", "企画提案 選定結果", True),
]
_HIROSHIMA_RESULT_WINDOW_DAYS = 365  # 選定結果は直近1年分のみ（全297件の古い履歴を抑制）
_HIROSHIMA_ROW = re.compile(
    r'<span class=article_title><a href="([^"]+)">([^<]+)</a></span>'
    r'\s*<span class=article_date>([^<]+)</span>', re.S)


def _scrape_hiroshima_sync() -> List[Dict]:
    import urllib.request
    import time as _time
    from urllib.parse import urljoin
    from datetime import date, timedelta
    op = urllib.request.build_opener()
    # データセンターIPにbot向けの空ページを返される対策として、実ブラウザ相当の
    # ヘッダ一式を送る（Accept / Accept-Language / Referer 等）。
    op.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
        ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        ("Accept-Language", "ja,en-US;q=0.9,en;q=0.8"),
        ("Referer", _HIROSHIMA_BASE + "/site/nyusatsukeiyaku/"),
        ("Connection", "keep-alive"),
    ]

    def _fetch(url):
        # 空ページ/一時失敗に備え最大3回リトライ（記事が取れたら即返す）
        last_err = None
        for attempt in range(3):
            try:
                doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
                if "article_title" in doc:
                    return doc
                last_err = "article_title無し(bot向け応答の疑い)"
            except Exception as e:  # noqa: BLE001
                last_err = e
            _time.sleep(2)
        raise RuntimeError(last_err)

    result_cutoff = (date.today() - timedelta(days=_HIROSHIMA_RESULT_WINDOW_DAYS)).isoformat()
    results, seen = [], set()
    for path, cat_label, is_result in _HIROSHIMA_CATEGORIES:
        try:
            html_doc = _fetch(_HIROSHIMA_BASE + path)
        except Exception as e:  # noqa: BLE001
            logger.error(f"広島県一覧取得失敗（{cat_label}）: {e}")
            continue
        for m in _HIROSHIMA_ROW.finditer(html_doc):
            href, title, date_raw = m.group(1), m.group(2).strip(), m.group(3)
            if not href.endswith(".html"):
                continue
            url = urljoin(_HIROSHIMA_BASE, href)
            if url in seen:
                continue
            title = re.sub(r"^【[^】]*】\s*", "", title).strip()
            # 広島の案件名は「〜の公募型プロポーザルを実施します」等で正規。
            # ハブ見出し（〜情報一覧）だけ除外し、過剰除外はしない。
            if len(title) < 4 or "情報一覧" in title:
                continue
            dm = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", date_raw)
            pub = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}" if dm else ""
            if is_result and pub and pub < result_cutoff:
                continue
            seen.add(url)
            cat = "プロポーザル" if re.search(r"プロポーザル|企画提案|公募型|企画競争", title + cat_label) else "入札"
            slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-") or str(len(seen))
            results.append({
                "title":           title,
                "category":        cat,
                "organization":    "広島県",
                "prefecture":      "広島県",
                "published_at":    "" if is_result else pub,
                "deadline":        "",
                "result_date":     pub if is_result else "",
                "result_url":      url if is_result else "",
                "project_code":    f"HIROSHIMA-{'R-' if is_result else ''}{slug}",
                "awardee":         "",
                "awardee_checked": "1" if is_result else "",
                "amount":          "",
                "url":             url,
                "source":          "HIROSHIMA",
                "source_category": cat_label,
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title)),
            })
    logger.info(f"広島県: {len(results)}件取得")
    return results


async def scrape_hiroshima() -> List[Dict]:
    """広島県公式サイトの入札公告一覧（物品・委託役務・電子入札）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_hiroshima_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"広島県スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 岡山県（新規）。県公式「業務委託等」「物品調達」がカテゴリのハブで、実案件は
# サブカテゴリ list328-XXXX / list355-XXXX に <li><span class="article_title"><a>案件名</a>…
# <span class="article_date">…日付…</span> の形で並ぶ。ハブ→サブ巡回で全件収集。
# ---------------------------------------------------------------------------
_OKAYAMA_BASE = "https://www.pref.okayama.jp"
_OKAYAMA_HUBS = ["/site/321/list328.html", "/site/321/list355.html"]
_OKAYAMA_ROW = re.compile(
    r'<li><span class="article_title"><a href="([^"]+)">([^<]+)</a>.*?'
    r'<span class="article_date">([^<]*)</span>', re.S)


def _scrape_okayama_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    # ハブからサブカテゴリ一覧URLを集める
    sublists = []
    for hub in _OKAYAMA_HUBS:
        try:
            hub_html = get(_OKAYAMA_BASE + hub)
        except Exception as e:  # noqa: BLE001
            logger.error(f"岡山県ハブ取得失敗（{hub}）: {e}")
            continue
        base_no = re.search(r"list(\d+)\.html", hub).group(1)
        for sp in sorted(set(re.findall(rf"/site/321/list{base_no}-\d+\.html", hub_html))):
            if sp not in sublists:
                sublists.append(sp)
        if hub not in sublists:  # ハブ自体にも案件がある場合に備え含める
            sublists.append(hub)

    results, seen = [], set()
    for sp in sublists:
        try:
            html_doc = get(_OKAYAMA_BASE + sp)
        except Exception:  # noqa: BLE001
            continue
        for m in _OKAYAMA_ROW.finditer(html_doc):
            href, title, date_raw = m.group(1), m.group(2).strip(), m.group(3)
            if "/site/321/" not in href or not href.endswith(".html"):
                continue
            url = urljoin(_OKAYAMA_BASE, href)
            if url in seen:
                continue
            seen.add(url)
            title = re.sub(r"^【[^】]*】\s*", "", title).strip()
            if len(title) < 4:
                continue
            dm = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", date_raw)
            pub = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}" if dm else ""
            is_result = bool(re.search(r"結果|落札", title))
            cat = "プロポーザル" if re.search(r"プロポーザル|企画提案|公募", title) else "入札"
            slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-") or str(len(seen))
            results.append({
                "title":           title,
                "category":        cat,
                "organization":    "岡山県",
                "prefecture":      "岡山県",
                "published_at":    "" if is_result else pub,
                "deadline":        "",
                "result_date":     pub if is_result else "",
                "result_url":      url if is_result else "",
                "project_code":    f"OKAYAMA-{'R-' if is_result else ''}{slug}",
                "awardee":         "",
                "awardee_checked": "1" if is_result else "",
                "amount":          "",
                "url":             url,
                "source":          "OKAYAMA",
                "source_category": "入札結果" if is_result else "",
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title)),
            })
    logger.info(f"岡山県: {len(results)}件取得")
    return results


async def scrape_okayama() -> List[Dict]:
    """岡山県公式サイトの入札公告（業務委託・物品の各カテゴリ）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_okayama_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"岡山県スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 愛媛県（新規）。県公式「入札情報（物品・委託等）」list92-339 は個別案件が主
# （article_title/article_date）。集約ページ（令和X年度…案件（物品）／発注情報／
# オープンカウンター等）は除外。物品の個別案件は当年度サマリー表(公告日/案件名/開札日)
# を展開して取得。直近window_daysで現行分に絞る。
# ---------------------------------------------------------------------------
_EHIME_BASE = "https://www.pref.ehime.jp"
_EHIME_LIST = _EHIME_BASE + "/site/nyusatsu/list92-339.html"
_EHIME_ROW = re.compile(
    r'article_title[^>]*><a href="([^"]+)">([^<]+)</a>.{0,150}?'
    r'article_date[^>]*>([^<]+)<', re.S)
# 集約・ハブページ（個別案件でない）を除外する
_EHIME_SKIP = re.compile(r"令和\d+年度.*案件（|発注情報（|オープンカウンター|掲載ページ|福祉施設からの物品の購入|入札・発注情報|一覧$")
_EHIME_WINDOW_DAYS = 120


def _ehime_wareki_iso(text: str) -> str:
    m = re.search(r"令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", text)
    if m:
        return f"{2018 + int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{4})\s*年\s*(\d+)\s*月\s*(\d+)\s*日", text)
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""


def _scrape_ehime_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    from datetime import date, timedelta
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    cutoff = (date.today() - timedelta(days=_EHIME_WINDOW_DAYS)).isoformat()
    try:
        html_doc = get(_EHIME_LIST)
    except Exception as e:  # noqa: BLE001
        logger.error(f"愛媛県一覧取得失敗: {e}")
        return []

    results, seen = [], set()
    buppin_summary_url = ""

    def _add(title, url, pub, cat, is_result, gyoshu=""):
        if url in seen:
            return
        seen.add(url)
        slug = re.sub(r"[^A-Za-z0-9]+", "-", url.rsplit("/", 1)[-1]).strip("-") or str(len(seen))
        results.append({
            "title": title, "category": cat, "organization": "愛媛県", "prefecture": "愛媛県",
            "published_at": "" if is_result else pub, "deadline": "",
            "result_date": pub if is_result else "", "result_url": url if is_result else "",
            "project_code": f"EHIME-{'R-' if is_result else ''}{slug}", "awardee": "",
            "awardee_checked": "1" if is_result else "",
            "amount": "", "url": url, "source": "EHIME",
            "source_category": gyoshu or ("入札結果" if is_result else ""),
            "summary": "", "detail": "", "tags": ",".join(generate_tags(title)),
        })

    for m in _EHIME_ROW.finditer(html_doc):
        href, raw_title, date_raw = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip(), m.group(3)
        title = re.sub(r"^（[^）]*更新）\s*", "", __import__("html").unescape(raw_title)).strip()
        pub = _ehime_wareki_iso(date_raw)
        # 当年度の物品サマリーは後で展開
        if re.search(r"令和\d+年度一般競争入札案件（物品）", title):
            if not buppin_summary_url:
                buppin_summary_url = urljoin(_EHIME_LIST, href)
            continue
        if _EHIME_SKIP.search(title):
            continue
        if pub and pub < cutoff:
            continue
        if len(title) < 5:
            continue
        is_result = bool(re.search(r"結果|落札", title))
        cat = "プロポーザル" if re.search(r"プロポーザル|企画提案|企画競争", title) else "入札"
        _add(title, urljoin(_EHIME_LIST, href), pub, cat, is_result)

    # 物品サマリー表（公告日 / 案件名[PDF] / 開札日 / 方式）を展開
    if buppin_summary_url:
        try:
            bh = get(buppin_summary_url)
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", bh, re.S | re.I):
                if "href" not in tr or "購入" not in tr and "製造" not in tr and "調達" not in tr and "借入" not in tr:
                    continue
                cells = [re.sub(r"\s+", " ", __import__("html").unescape(re.sub(r"<[^>]+>", "", c))).strip()
                         for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                cells = [c for c in cells if c]
                am = re.search(r'href="([^"]+)"', tr)
                if len(cells) < 2 or not am:
                    continue
                pub = _ehime_wareki_iso(cells[0])
                name = re.sub(r"\s*\[PDF[^\]]*\].*$", "", cells[1]).strip()
                if not name or (pub and pub < cutoff):
                    continue
                _add(name, urljoin(buppin_summary_url, am.group(1)), pub, "入札", False, gyoshu="物品")
        except Exception as e:  # noqa: BLE001
            logger.error(f"愛媛県物品サマリー展開失敗: {e}")

    logger.info(f"愛媛県: {len(results)}件取得")
    return results


async def scrape_ehime() -> List[Dict]:
    """愛媛県公式サイトの入札情報（物品・委託等）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_ehime_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"愛媛県スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 高知県（新規）。県公式「入札情報」カテゴリに個別公告が <a href="/doc/YYYYMMDD…/">案件名</a>
# で並ぶ。doc IDの先頭8桁が日付。港湾・道路・委託業務が多い。直近window日で現行分に絞る。
# ---------------------------------------------------------------------------
_KOCHI_BASE = "https://www.pref.kochi.lg.jp"
_KOCHI_CAT = _KOCHI_BASE + "/category/bunya/shigoto_sangyo/nyusatsujoho/"
# トップは直近の混在一覧。各サブカテゴリの more@docs_1.html が全件一覧。
# (hint: プロポ扱いにするか) の順で巡回し、doc IDの日付でwindow内に絞る。
_KOCHI_LISTS = [
    (_KOCHI_CAT, False),
    (_KOCHI_CAT + "buppinchotatsujoho/", False),                       # 物品調達
    (_KOCHI_CAT + "ippankyosonyusatsu/more@docs_1.html", False),        # 一般競争入札(工事・委託)
    (_KOCHI_CAT + "ippankyosonyusatsu_proposal/more@docs_1.html", True),  # プロポーザル
    (_KOCHI_CAT + "ippankyosonyusatsu_proposal_ninidantai/", True),     # 任意団体プロポ
    (_KOCHI_CAT + "kenyuchi/", False),                                  # 県有地
]
_KOCHI_ROW = re.compile(r'<a href="(/doc/(\d{8})\d+/?)"[^>]*>([^<]+)</a>')
_KOCHI_WINDOW_DAYS = 120


def _scrape_kochi_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    from datetime import date, timedelta
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    cutoff = (date.today() - timedelta(days=_KOCHI_WINDOW_DAYS)).isoformat()
    import html as _html
    results, seen = [], set()
    for list_url, proposal_hint in _KOCHI_LISTS:
        try:
            html_doc = op.open(list_url, timeout=40).read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            logger.error(f"高知県一覧取得失敗（{list_url}）: {e}")
            continue
        for m in _KOCHI_ROW.finditer(html_doc):
            href, ymd, raw_title = m.group(1), m.group(2), _html.unescape(m.group(3)).strip()
            pub = f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}"
            if not re.search(r"公告|入札|委託|プロポ|企画提案|調達|結果|募集|工事|業務", raw_title):
                continue
            if pub < cutoff:  # 古い常設ページ等を除外
                continue
            url = urljoin(_KOCHI_BASE, href)
            if url in seen:
                continue
            seen.add(url)
            title = re.sub(r"^(?:\d+月\d+日[^【]*)?【[^】]*】\s*", "", raw_title).strip()
            title = re.sub(r"^【[^】]*】\s*", "", title).strip()
            if len(title) < 5:
                continue
            is_result = bool(re.search(r"入札結果|落札|開札結果|結果について", raw_title))
            cat = "プロポーザル" if (proposal_hint or re.search(r"プロポーザル|企画提案|企画競争", raw_title)) else "入札"
            slug = ymd + re.sub(r"\D", "", href)[-5:]
            results.append({
            "title": title, "category": cat, "organization": "高知県", "prefecture": "高知県",
            "published_at": "" if is_result else pub, "deadline": "",
            "result_date": pub if is_result else "", "result_url": url if is_result else "",
            "project_code": f"KOCHI-{'R-' if is_result else ''}{slug}", "awardee": "",
            "awardee_checked": "1" if is_result else "",
            "amount": "", "url": url, "source": "KOCHI",
            "source_category": "入札結果" if is_result else "",
            "summary": "", "detail": "", "tags": ",".join(generate_tags(title)),
        })
    logger.info(f"高知県: {len(results)}件取得")
    return results


async def scrape_kochi() -> List[Dict]:
    """高知県公式サイトの入札情報（公告一覧）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_kochi_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"高知県スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 佐賀県（新規）。県公式のカテゴリ別一覧：<li><span class="upddate">2026年7月17日更新
# </span><div class="title"><a href="kijiXXXX/index.html">案件名</a>。委託・物品を巡回。
# ---------------------------------------------------------------------------
_SAGA_BASE = "https://www.pref.saga.lg.jp"
# (list番号, class_id, 表示名)。1ページ目は本体HTML、2ページ目以降は
# hpkijilistpagerhandler.ashx?class_id=…&pg=N（Referer必須）で全件取得。
_SAGA_CATEGORIES = [
    ("02043", "2043", "委託・役務"),
    ("02059", "2059", "物品"),
]
_SAGA_PAGER = (_SAGA_BASE +
               "/dynamic/hpkiji/pub/hpkijilistpagerhandler.ashx"
               "?c_id=3&class_id={cls}&class_set_id=1&pg={pg}&kbn=kijilist&top_id=0")
_SAGA_ROW = re.compile(
    r'<span class="upddate">(\d{4})年(\d{1,2})月(\d{1,2})日更新</span>\s*'
    r'<div class="title">\s*<a href="([^"]+)">([^<]+)</a>', re.S)
_SAGA_WINDOW_DAYS = 180


def _scrape_saga_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    from datetime import date, timedelta
    import html as _html
    cutoff = (date.today() - timedelta(days=_SAGA_WINDOW_DAYS)).isoformat()
    results, seen = [], set()
    for list_no, cls, cat_label in _SAGA_CATEGORIES:
        top_url = f"{_SAGA_BASE}/list{list_no}.html"

        def _fetch(url, ref=None):
            hd = {"User-Agent": "Mozilla/5.0"}
            if ref:
                hd["Referer"] = ref
                hd["X-Requested-With"] = "XMLHttpRequest"
            req = urllib.request.Request(url, headers=hd)
            return urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "replace")

        # 全ページのHTMLを集める（pg1=本体, pg2..=AJAXハンドラ）
        pages = []
        try:
            pages.append(_fetch(top_url))
        except Exception as e:  # noqa: BLE001
            logger.error(f"佐賀県一覧取得失敗（{cat_label}）: {e}")
            continue
        for pg in range(2, 40):
            try:
                h = _fetch(_SAGA_PAGER.format(cls=cls, pg=pg), ref=top_url)
            except Exception:  # noqa: BLE001
                break
            if not _SAGA_ROW.search(h):
                break
            pages.append(h)

        for html_doc in pages:
            for m in _SAGA_ROW.finditer(html_doc):
                y, mo, d, href, title = m.group(1), m.group(2), m.group(3), m.group(4), _html.unescape(m.group(5)).strip()
                url = urljoin(top_url, href)
                if url in seen:
                    continue
                pub = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
                if pub < cutoff:  # 古い案件は除外
                    continue
                seen.add(url)
                if len(title) < 5 or "随意契約の契約内容" in title:
                    continue
                is_result = bool(re.search(r"決定しました|落札|入札結果|結果について", title))
                cat = "プロポーザル" if re.search(r"プロポーザル|企画提案|企画競争", title) else "入札"
                slug = re.sub(r"[^A-Za-z0-9]+", "-", href.split("/", 1)[0]).strip("-") or str(len(seen))
                results.append({
                    "title": title, "category": cat, "organization": "佐賀県", "prefecture": "佐賀県",
                    "published_at": "" if is_result else pub, "deadline": "",
                    "result_date": pub if is_result else "", "result_url": url if is_result else "",
                    "project_code": f"SAGA-{'R-' if is_result else ''}{slug}", "awardee": "",
                    "awardee_checked": "1" if is_result else "",
                    "amount": "", "url": url, "source": "SAGA",
                    "source_category": cat_label + (" 結果" if is_result else ""),
                    "summary": "", "detail": "", "tags": ",".join(generate_tags(title)),
                })
    logger.info(f"佐賀県: {len(results)}件取得")
    return results


async def scrape_saga() -> List[Dict]:
    """佐賀県公式サイトの入札公告（委託・物品）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_saga_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"佐賀県スクレイパー例外: {e}")
        return []


def fetch_saga_detail(url: str) -> Optional[Dict]:
    """佐賀県 入札公告 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"佐賀県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="tmp_contents") or soup.find(id="main_body") or soup.find("main") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 島根県（新規）。県公式 入札情報の「過去の入札情報一覧」(rireki_list.html)が
# 全部局の公告を集約した静的リスト（週末に更新・履歴保持）。1行=
# <li><a href="/bid_info/bid_XXX/YYY.html">【機関名】案件名について掲載しました</a>（M月D日）</li>
# ---------------------------------------------------------------------------
_SHIMANE_BASE = "https://www.pref.shimane.lg.jp"
_SHIMANE_LIST = _SHIMANE_BASE + "/bid_info/rireki_list.html"
_SHIMANE_ROW = re.compile(
    r'<a href="(/bid_info/[^"]+\.html)"[^>]*>([^<]+)</a>\s*（\s*(\d{1,2})月(\d{1,2})日）')


def _scrape_shimane_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    from datetime import date, timedelta
    import html as _html
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    try:
        html_doc = op.open(_SHIMANE_LIST, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"島根県一覧取得失敗: {e}")
        return []
    ul = re.search(r'<ul class="genre-news">(.*?)</ul>', html_doc, re.S)
    body = ul.group(1) if ul else html_doc
    today = date.today()
    results, seen = [], set()
    for m in _SHIMANE_ROW.finditer(body):
        href, raw, mo, d = m.group(1), _html.unescape(m.group(2)).strip(), int(m.group(3)), int(m.group(4))
        url = urljoin(_SHIMANE_BASE, href)
        if url in seen:
            continue
        seen.add(url)
        org_m = re.match(r"【([^】]+)】", raw)
        org = org_m.group(1) if org_m else "島根県"
        title = re.sub(r"^【[^】]*】", "", raw)
        title = re.sub(r"(について|に係る情報|を)?(掲載|公表|更新|公告)し(ました|ます)。?$", "", title).strip()
        title = re.sub(r"について$", "", title).strip()
        if len(title) < 4:
            continue
        # 年の推定：月が未来なら前年（12月案件が翌年前半の一覧に残るケース）
        y = today.year
        try:
            cand = date(y, mo, d)
            if (cand - today).days > 30:
                cand = date(y - 1, mo, d)
        except ValueError:
            continue
        pub = cand.isoformat()
        is_result = bool(re.search(r"入札結果|落札者|開札結果|結果について|選定結果|選定しました", raw))
        cat = "プロポーザル" if re.search(r"プロポ|提案競技|企画競争|企画提案|公募型", raw) else "入札"
        slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 2)[-1]).strip("-") or str(len(seen))
        results.append({
            "title": title, "category": cat,
            "organization": f"島根県（{org}）" if org != "島根県" else "島根県",
            "prefecture": "島根県",
            "published_at": "" if is_result else pub, "deadline": "",
            "result_date": pub if is_result else "", "result_url": url if is_result else "",
            "project_code": f"SHIMANE-{'R-' if is_result else ''}{slug}", "awardee": "",
            "awardee_checked": "1" if is_result else "",
            "amount": "", "url": url, "source": "SHIMANE",
            "source_category": "入札結果" if is_result else "",
            "summary": "", "detail": "", "tags": ",".join(generate_tags(title, org)),
        })
    logger.info(f"島根県: {len(results)}件取得")
    return results


async def scrape_shimane() -> List[Dict]:
    """島根県公式サイトの入札情報（過去の入札情報一覧）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_shimane_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"島根県スクレイパー例外: {e}")
        return []


def fetch_shimane_detail(url: str) -> Optional[Dict]:
    """島根県 入札公告 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"島根県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="page-content") or soup.find("main") or soup.find(id="honbun") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 熊本県（新規）。県公式 入札情報の一覧 /life/sub/5/index-2.html が全部局の
# 入札・公募を集約した静的リスト（広島と同じCMS：article_date/article_title）。
# 個別案件は /soshiki/<部局>/<記事id>.html。
# ---------------------------------------------------------------------------
_KUMAMOTO_BASE = "https://www.pref.kumamoto.jp"
# (URL, strict)。index-2=入札情報課の集約(全件)。list1-8=全庁新着で部局横断の
# 公募・プロポを拾う（strict=入札/公募系キーワードのみ採用しニュースを除外）。
_KUMAMOTO_LISTS = [
    ("/life/sub/5/index-2.html", False),
    ("/soshiki/list1-8.html", True),
]
_KUMAMOTO_STRICT = re.compile(
    r"入札|公告|公募|プロポ|提案競技|企画競争|企画提案|委託|調達|見積|売却|落札|開札")
_KUMAMOTO_ROW = re.compile(
    r'<span class="article_date">(\d{4})年(\d{1,2})月(\d{1,2})日更新</span>\s*'
    r'<span class="article_title">\s*<a href="([^"]+)">([^<]+)</a>', re.S)


def _scrape_kumamoto_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    import html as _html
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    results, seen = [], set()
    for path, strict in _KUMAMOTO_LISTS:
        try:
            html_doc = op.open(_KUMAMOTO_BASE + path, timeout=40).read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            logger.error(f"熊本県一覧取得失敗（{path}）: {e}")
            continue
        for m in _KUMAMOTO_ROW.finditer(html_doc):
            y, mo, d, href, raw = m.group(1), m.group(2), m.group(3), m.group(4), _html.unescape(m.group(5)).strip()
            if strict and not _KUMAMOTO_STRICT.search(raw):
                continue
            url = urljoin(_KUMAMOTO_BASE, href)
            if url in seen:
                continue
            seen.add(url)
            # 先頭の【…】注記（更新日・募集終了等）を除去して案件名を出す
            title = re.sub(r"^(?:【[^】]*】)+", "", raw).strip()
            title = re.sub(r"について$", "", title).strip()
            if len(title) < 5:
                continue
            pub = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
            is_result = bool(re.search(r"結果|募集終了|選定しました|落札者|開札結果", raw))
            cat = "プロポーザル" if re.search(r"プロポ|提案競技|企画競争|企画提案|公募型", raw) else "入札"
            slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 2)[-1]).strip("-") or str(len(seen))
            results.append({
                "title": title, "category": cat, "organization": "熊本県", "prefecture": "熊本県",
                "published_at": "" if is_result else pub, "deadline": "",
                "result_date": pub if is_result else "", "result_url": url if is_result else "",
                "project_code": f"KUMAMOTO-{'R-' if is_result else ''}{slug}", "awardee": "",
                "awardee_checked": "1" if is_result else "",
                "amount": "", "url": url, "source": "KUMAMOTO",
                "source_category": "入札結果" if is_result else "",
                "summary": "", "detail": "", "tags": ",".join(generate_tags(title)),
            })
    logger.info(f"熊本県: {len(results)}件取得")
    return results


async def scrape_kumamoto() -> List[Dict]:
    """熊本県公式サイトの入札情報一覧（全部局集約）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_kumamoto_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"熊本県スクレイパー例外: {e}")
        return []


def fetch_kumamoto_detail(url: str) -> Optional[Dict]:
    """熊本県 入札・公募 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"熊本県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="page-content") or soup.find("main") or soup.find(id="honbun") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 北海道（新規）。道公式「入札情報」/news/nyusatsu/ が全部局の入札公告・公募・
# プロポを集約した静的リスト（記事CMS）。<article><time datetime><h2><a>案件名。
# 直近100件（約60日分）。入札予定・結果等の公表(ハブ)は除外。
# ---------------------------------------------------------------------------
_HOKKAIDO_BASE = "https://www.pref.hokkaido.lg.jp"
_HOKKAIDO_LIST = _HOKKAIDO_BASE + "/news/nyusatsu/"
_HOKKAIDO_ROW = re.compile(
    r'<time datetime="(\d{4}-\d{2}-\d{2})"[^>]*>[^<]*</time>\s*'
    r'<h2>\s*<a href="([^"]+)">([^<]+)</a>', re.S)
_HOKKAIDO_HUB = re.compile(r"^入札(予定|結果|案内|等)|結果等の公表|入札案内$|の公表$|の公表について|過去の入札")


def _scrape_hokkaido_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    import html as _html
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    try:
        html_doc = op.open(_HOKKAIDO_LIST, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"北海道一覧取得失敗: {e}")
        return []
    results, seen = [], set()
    for m in _HOKKAIDO_ROW.finditer(html_doc):
        pub, href, raw = m.group(1), m.group(2), _html.unescape(m.group(3)).strip()
        url = urljoin(_HOKKAIDO_BASE, href)
        if url in seen:
            continue
        seen.add(url)
        # 【受付終了】【終了しました】【告示】等の注記を除去
        title = re.sub(r"^(?:【[^】]*】)+", "", raw).strip()
        if _HOKKAIDO_HUB.search(title) or len(title) < 5:
            continue
        is_result = bool(re.search(r"落札者|開札結果|入札結果", raw)) or "【募集終了】" in raw or "【終了しました】" in raw or "【受付終了】" in raw
        cat = "プロポーザル" if re.search(r"プロポ|提案競技|企画競争|企画提案|公募型", raw) else "入札"
        slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 2)[-1]).strip("-") or str(len(seen))
        results.append({
            "title": title, "category": cat, "organization": "北海道", "prefecture": "北海道",
            "published_at": "" if is_result else pub, "deadline": "",
            "result_date": pub if is_result else "", "result_url": url if is_result else "",
            "project_code": f"HOKKAIDO-{'R-' if is_result else ''}{slug}", "awardee": "",
            "awardee_checked": "1" if is_result else "",
            "amount": "", "url": url, "source": "HOKKAIDO",
            "source_category": "入札結果" if is_result else "",
            "summary": "", "detail": "", "tags": ",".join(generate_tags(title)),
        })
    logger.info(f"北海道: {len(results)}件取得")
    return results


async def scrape_hokkaido() -> List[Dict]:
    """北海道公式サイトの入札情報一覧（全部局集約）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_hokkaido_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"北海道スクレイパー例外: {e}")
        return []


def fetch_hokkaido_detail(url: str) -> Optional[Dict]:
    """北海道 入札・公募 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"北海道詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="page-content") or soup.find("main") or soup.find(class_="contents") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 徳島県（新規）。県公式「入札・調達・売却に関する新着情報」が全部局横断の集約
# フィード（静的）。<time datetime><span class=title><a>案件名</a><span class=belong>(分類)。
# ---------------------------------------------------------------------------
_TOKUSHIMA_LIST = "https://www.pref.tokushima.lg.jp/mokuteki/nyusatsu/news/"
_TOKUSHIMA_ROW = re.compile(
    r'<time\s+datetime="(\d{4}-\d{2}-\d{2})[^"]*">[^<]*</time>\s*'
    r'<span class="title">\s*<a href="([^"]+)">([^<]+)</a>\s*</span>\s*'
    r'(?:<span class="belong">\(([^)]+)\)</span>)?', re.S)


def _scrape_tokushima_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    import html as _html
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    try:
        html_doc = op.open(_TOKUSHIMA_LIST, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"徳島県一覧取得失敗: {e}")
        return []
    results, seen = [], set()
    for m in _TOKUSHIMA_ROW.finditer(html_doc):
        pub, href, raw, belong = m.group(1), m.group(2), _html.unescape(m.group(3)).strip(), (m.group(4) or "").strip()
        url = urljoin(_TOKUSHIMA_LIST, href)
        if url in seen:
            continue
        seen.add(url)
        title = re.sub(r"^(?:【[^】]*】)+", "", raw).strip()
        if len(title) < 5:
            continue
        is_result = bool(re.search(r"落札|開札結果|入札結果|選定結果|結果について", raw))
        cat = "プロポーザル" if re.search(r"プロポ|提案競技|企画競争|企画提案|公募型", raw + belong) else "入札"
        slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rstrip("/").rsplit("/", 1)[-1]).strip("-") or str(len(seen))
        results.append({
            "title": title, "category": cat, "organization": "徳島県", "prefecture": "徳島県",
            "published_at": "" if is_result else pub, "deadline": "",
            "result_date": pub if is_result else "", "result_url": url if is_result else "",
            "project_code": f"TOKUSHIMA-{'R-' if is_result else ''}{slug}", "awardee": "",
            "awardee_checked": "1" if is_result else "",
            "amount": "", "url": url, "source": "TOKUSHIMA",
            "source_category": (belong + " 結果") if is_result else belong,
            "summary": "", "detail": "", "tags": ",".join(generate_tags(title)),
        })
    logger.info(f"徳島県: {len(results)}件取得")
    return results


async def scrape_tokushima() -> List[Dict]:
    """徳島県公式サイトの入札・調達・売却の新着情報（全部局集約）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_tokushima_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"徳島県スクレイパー例外: {e}")
        return []


def fetch_tokushima_detail(url: str) -> Optional[Dict]:
    """徳島県 入札・公募 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"徳島県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="page-content") or soup.find("main") or soup.find(class_="contents") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 長崎県（新規）。県公式「入札情報」/nyusatsu-docs/ が業務委託・売却の公告/結果を
# 集約した静的フィード（<li><time datetime><a>案件名</a><span class=category>分類）。
# category末尾が「公告」=入札公告、「結果」=応札結果。業務委託・プロポが充実。
# ---------------------------------------------------------------------------
_NAGASAKI_LIST = "https://www.pref.nagasaki.jp/nyusatsu-docs/"
_NAGASAKI_ROW = re.compile(
    r'<li>\s*<time datetime="(\d{4}-\d{2}-\d{2})[^"]*">[^<]*</time>\s*'
    r'<a href="([^"]+)">([^<]+)</a>\s*<span class="category">([^<]*)</span>', re.S)


def _scrape_nagasaki_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    import html as _html
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    try:
        html_doc = op.open(_NAGASAKI_LIST, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"長崎県一覧取得失敗: {e}")
        return []
    results, seen = [], set()
    for m in _NAGASAKI_ROW.finditer(html_doc):
        pub, href, raw, cat_label = m.group(1), m.group(2), _html.unescape(m.group(3)).strip(), m.group(4).strip()
        url = urljoin(_NAGASAKI_LIST, href)
        if url in seen:
            continue
        seen.add(url)
        title = re.sub(r"^(?:【[^】]*】)+", "", raw).strip()
        if len(title) < 5:
            continue
        is_result = "結果" in cat_label or bool(re.search(r"落札|開札結果|入札結果", raw))
        cat = "プロポーザル" if re.search(r"プロポ|提案競技|企画競争|企画提案|公募型", raw + cat_label) else "入札"
        slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-") or str(len(seen))
        results.append({
            "title": title, "category": cat, "organization": "長崎県", "prefecture": "長崎県",
            "published_at": "" if is_result else pub, "deadline": "",
            "result_date": pub if is_result else "", "result_url": url if is_result else "",
            "project_code": f"NAGASAKI-{'R-' if is_result else ''}{slug}", "awardee": "",
            "awardee_checked": "1" if is_result else "",
            "amount": "", "url": url, "source": "NAGASAKI",
            "source_category": cat_label,
            "summary": "", "detail": "", "tags": ",".join(generate_tags(title)),
        })
    logger.info(f"長崎県: {len(results)}件取得")
    return results


async def scrape_nagasaki() -> List[Dict]:
    """長崎県公式サイトの入札情報（業務委託・売却の公告/結果）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_nagasaki_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"長崎県スクレイパー例外: {e}")
        return []


def fetch_nagasaki_detail(url: str) -> Optional[Dict]:
    """長崎県 入札・公募 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"長崎県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="page-content") or soup.find("main") or soup.find(class_="contents") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 沖縄県（新規）。公募・入札発注情報ハブ(1015342)配下の13カテゴリ索引に、現年度
# (令和8年度)の個別案件が直接並ぶ(末尾 NNN.html)。一覧に日付が無いため案件ページの
# 「更新日」を取得。工事(電子入札ポータル 1015344)は静的外なので除外。
# ---------------------------------------------------------------------------
_OKINAWA_HUB = "https://www.pref.okinawa.jp/shigoto/nyusatsukeiyaku/1015342/index.html"
_OKINAWA_CUR_FY = ["令和8年度", "令和８年度"]  # 現年度（毎年度更新が必要）
_OKINAWA_MAX_DETAIL = 400


def _scrape_okinawa_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    import html as _html
    import time as _time
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(u):
        try:
            return op.open(u, timeout=30).read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""

    hub = get(_OKINAWA_HUB)
    if not hub:
        logger.error("沖縄県ハブ取得失敗")
        return []
    cats = sorted(set(re.findall(r"/shigoto/nyusatsukeiyaku/1015342/(\d+)/index\.html", hub)))
    cats = [c for c in cats if c != "1015344"]  # 建設工事(電子入札)は除外

    # 各カテゴリ索引から現年度の個別案件(末尾NNN.html)を収集。
    # 併せて「令和8年度実施業務（…）」サマリ索引ページも辿り、その中の案件も収集。
    cases = {}
    year_pages = set()
    for cid in cats:
        ci = f"https://www.pref.okinawa.jp/shigoto/nyusatsukeiyaku/1015342/{cid}/index.html"
        ch = get(ci)
        for href, title in re.findall(r'<a href="([^"]+?/(?:\d+\.html|index\.html))">([^<]+)</a>', ch):
            t = _html.unescape(title).strip()
            if not any(fy in t for fy in _OKINAWA_CUR_FY):
                continue
            url = urljoin(ci, href)
            if url.endswith("/index.html"):
                year_pages.add(url)  # 令和8年度サマリ索引 → 後で中の案件を収集
            elif url not in cases:
                cases[url] = t
    for yu in year_pages:
        yh = get(yu)
        ul = re.search(r'<ul class="listlink[^"]*">(.*?)</ul>', yh, re.S)
        block = ul.group(1) if ul else yh
        for href, title in re.findall(r'<a href="([^"]+?/\d+\.html)">([^<]{6,})</a>', block):
            t = _html.unescape(title).strip()
            if re.search(r"公告|入札|プロポ|委託|募集|提案|見積|調達|業務|購入|賃貸借|売払", t):
                url = urljoin(yu, href)
                if url not in cases:
                    cases[url] = t

    results = []
    budget = _OKINAWA_MAX_DETAIL
    for url, raw in cases.items():
        pub = ""
        if budget > 0:
            page = get(url)
            budget -= 1
            dm = re.search(r"(?:更新日|公開日)[：:\s]*(20\d\d)年(\d{1,2})月(\d{1,2})", page)
            if dm:
                pub = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
            _time.sleep(0.12)
        if not pub:
            continue  # 日付が取れない案件は品質確保のため採用しない
        title = re.sub(r"^(?:【[^】]*】)+", "", raw).strip()
        if len(title) < 5:
            continue
        is_result = bool(re.search(r"落札|開札結果|入札結果|選定結果|結果について|結果公表", raw))
        cat = "プロポーザル" if re.search(r"プロポ|提案競技|企画競争|企画提案|公募型|企画公募", raw) else "入札"
        slug = re.sub(r"[^0-9]", "", url.rsplit("/", 1)[-1]) or str(len(results))
        results.append({
            "title": title, "category": cat, "organization": "沖縄県", "prefecture": "沖縄県",
            "published_at": "" if is_result else pub, "deadline": "",
            "result_date": pub if is_result else "", "result_url": url if is_result else "",
            "project_code": f"OKINAWA-{'R-' if is_result else ''}{slug}", "awardee": "",
            "awardee_checked": "1" if is_result else "",
            "amount": "", "url": url, "source": "OKINAWA",
            "source_category": "",
            "summary": "", "detail": "", "tags": ",".join(generate_tags(title)),
        })
    logger.info(f"沖縄県: {len(results)}件取得")
    return results


async def scrape_okinawa() -> List[Dict]:
    """沖縄県公式サイトの公募・入札発注情報（現年度の委託・プロポ等）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_okinawa_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"沖縄県スクレイパー例外: {e}")
        return []


def fetch_okinawa_detail(url: str) -> Optional[Dict]:
    """沖縄県 公募・入札 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"沖縄県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="page-content") or soup.find("main") or soup.find(class_="contents") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 大分県（新規）。県公式「入札・公募情報」/site/nyusatu-koubo/ のカテゴリ別一覧
# （広島と同じ article_date/article_title CMS）。企画提案(プロポ)が中心。
# ※大分のサーバー証明書チェーンが不完全でTLS検証が失敗するため未検証контекストを使用。
# ---------------------------------------------------------------------------
_OITA_BASE = "https://www.pref.oita.jp"
_OITA_CATEGORIES = [
    ("/site/nyusatu-koubo/list22380-29038.html", "企画提案", False),
    ("/site/nyusatu-koubo/list22377-29036.html", "調査・委託", False),
    ("/site/nyusatu-koubo/list22377-29035.html", "土木・建築・設備", False),
    ("/site/nyusatu-koubo/list22377-29037.html", "物品", False),
    ("/site/nyusatu-koubo/list22377-29227.html", "その他", False),
    ("/site/nyusatu-koubo/list22381-29228.html", "入札結果", True),
]
_OITA_ROW = re.compile(
    r'<span class="article_date">(\d{4})年(\d{1,2})月(\d{1,2})日更新</span>\s*'
    r'<span class="article_title">\s*<a href="([^"]+)">([^<]+)</a>', re.S)


def _scrape_oita_sync() -> List[Dict]:
    import urllib.request
    import ssl as _ssl
    from urllib.parse import urljoin
    import html as _html
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE  # 大分は証明書チェーン不完全のため検証無効
    op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    results, seen = [], set()
    for path, cat_label, force_result in _OITA_CATEGORIES:
        try:
            html_doc = op.open(_OITA_BASE + path, timeout=40).read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            logger.error(f"大分県一覧取得失敗（{cat_label}）: {e}")
            continue
        for m in _OITA_ROW.finditer(html_doc):
            y, mo, d, href, raw = m.group(1), m.group(2), m.group(3), m.group(4), _html.unescape(m.group(5)).strip()
            url = urljoin(_OITA_BASE, href)
            if url in seen:
                continue
            seen.add(url)
            title = re.sub(r"^(?:【[^】]*】)+", "", raw).strip()
            if len(title) < 5:
                continue
            pub = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
            is_result = force_result or bool(re.search(r"結果|候補者の決定|落札者|開札結果|選定しました", raw))
            cat = "プロポーザル" if re.search(r"プロポ|提案競技|企画競争|企画提案|公募型", raw + cat_label) else "入札"
            slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-") or str(len(seen))
            results.append({
                "title": title, "category": cat, "organization": "大分県", "prefecture": "大分県",
                "published_at": "" if is_result else pub, "deadline": "",
                "result_date": pub if is_result else "", "result_url": url if is_result else "",
                "project_code": f"OITA-{'R-' if is_result else ''}{slug}", "awardee": "",
                "awardee_checked": "1" if is_result else "",
                "amount": "", "url": url, "source": "OITA",
                "source_category": (cat_label + " 結果") if (is_result and cat_label != "入札結果") else cat_label,
                "summary": "", "detail": "", "tags": ",".join(generate_tags(title)),
            })
    logger.info(f"大分県: {len(results)}件取得")
    return results


async def scrape_oita() -> List[Dict]:
    """大分県公式サイトの入札・公募情報（企画提案・委託等）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_oita_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"大分県スクレイパー例外: {e}")
        return []


def fetch_oita_detail(url: str) -> Optional[Dict]:
    """大分県 入札・公募 個別ページの本文を取得する。"""
    import urllib.request
    import ssl as _ssl
    from urllib.parse import urljoin
    try:
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"大分県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="tmp_contents") or soup.find(id="page-content") or soup.find("main") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 秋田県（新規）。美の国あきたネットの「コンペ情報」「その他の入札」ジャンル一覧。
# <a href="/pages/archive/N">案件名</a> [<time datetime="YYYY-MM-DD">]。企画提案競技中心。
# ---------------------------------------------------------------------------
_AKITA_BASE = "https://www.pref.akita.lg.jp"
_AKITA_GENRES = [("12231", "コンペ"), ("12229", "その他入札")]
_AKITA_ROW = re.compile(
    r'<a href="([^"]*?/pages/archive/\d+)"[^>]*>([^<]+)</a>\s*(?:&nbsp;)?\s*'
    r'\[<time datetime="(\d{4}-\d{2}-\d{2})"', re.S)


def _scrape_akita_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    import html as _html
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    results, seen = [], set()
    for genre, cat_label in _AKITA_GENRES:
        try:
            html_doc = op.open(f"{_AKITA_BASE}/pages/genre/{genre}", timeout=40).read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            logger.error(f"秋田県一覧取得失敗（{cat_label}）: {e}")
            continue
        for m in _AKITA_ROW.finditer(html_doc):
            href, raw, pub = m.group(1), _html.unescape(m.group(2)).strip(), m.group(3)
            url = urljoin(_AKITA_BASE, href)
            if url in seen:
                continue
            seen.add(url)
            title = re.sub(r"^(?:【[^】]*】)+", "", raw).strip()
            if len(title) < 5:
                continue
            is_result = bool(re.search(r"審査結果|選定結果|結果について|落札者|開札結果|選定しました|決定しました", raw))
            cat = "プロポーザル" if re.search(r"プロポ|企画提案|提案競技|企画競争|コンペ|公募型", raw + cat_label) else "入札"
            slug = re.sub(r"[^0-9]", "", href.rsplit("/", 1)[-1]) or str(len(seen))
            results.append({
                "title": title, "category": cat, "organization": "秋田県", "prefecture": "秋田県",
                "published_at": "" if is_result else pub, "deadline": "",
                "result_date": pub if is_result else "", "result_url": url if is_result else "",
                "project_code": f"AKITA-{'R-' if is_result else ''}{slug}", "awardee": "",
                "awardee_checked": "1" if is_result else "",
                "amount": "", "url": url, "source": "AKITA",
                "source_category": (cat_label + " 結果") if is_result else cat_label,
                "summary": "", "detail": "", "tags": ",".join(generate_tags(title)),
            })
    logger.info(f"秋田県: {len(results)}件取得")
    return results


async def scrape_akita() -> List[Dict]:
    """秋田県公式サイト（美の国あきたネット）のコンペ・入札情報を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_akita_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"秋田県スクレイパー例外: {e}")
        return []


def fetch_akita_detail(url: str) -> Optional[Dict]:
    """秋田県 コンペ・入札 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"秋田県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="tmp_contents") or soup.find(id="page-content") or soup.find("main") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 福島県（新規）。県公式「入札・契約情報」/sec/list10-N.html が全部局横断の集約
# フィード（<span class=span_a>日付</span><span class=span_b><a>案件名</a>（<a>部局</a>））。
# 発注見通し・契約結果ハブ等の常設ページは除外。
# ---------------------------------------------------------------------------
_FUKUSHIMA_BASE = "https://www.pref.fukushima.lg.jp/sec/"
_FUKUSHIMA_MAX_PAGES = 10
_FUKUSHIMA_ROW = re.compile(
    r'<span class="span_a">(\d{4})年(\d{1,2})月(\d{1,2})日更新</span>\s*'
    r'<span class="span_b">\s*<a href="([^"]+)">([^<]+)</a>'
    r'(?:（<a href="[^"]*">([^<]+)</a>）)?', re.S)
_FUKUSHIMA_HUB = re.compile(
    r"発注見通し|見通しを(?:掲載|公表)|契約結果|^入札結果|結果を公表|入札情報一覧|"
    r"入札情報（|^入札・?契約情報|お知らせ一覧|一覧を(?:掲載|更新)|情報を更新")


def _scrape_fukushima_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    import html as _html
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    results, seen = [], set()
    for p in range(1, _FUKUSHIMA_MAX_PAGES + 1):
        try:
            html_doc = op.open(f"{_FUKUSHIMA_BASE}list10-{p}.html", timeout=40).read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            break
        rows = list(_FUKUSHIMA_ROW.finditer(html_doc))
        if not rows:
            break
        for m in rows:
            y, mo, d, href, raw, org = m.group(1), m.group(2), m.group(3), m.group(4), _html.unescape(m.group(5)).strip(), (m.group(6) or "").strip()
            title = re.sub(r"^(?:【[^】]*】)+", "", raw).strip()
            if _FUKUSHIMA_HUB.search(raw) or len(title) < 5:
                continue
            url = urljoin(_FUKUSHIMA_BASE, href)
            if url in seen:
                continue
            seen.add(url)
            pub = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
            is_result = bool(re.search(r"審査結果|選定結果|結果について|落札者|開札結果|選定しました|決定しました", raw))
            cat = "プロポーザル" if re.search(r"プロポ|提案競技|企画競争|企画提案|公募型", raw) else "入札"
            slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1].replace(".html", "")).strip("-") or str(len(seen))
            results.append({
                "title": title, "category": cat,
                "organization": f"福島県（{org}）" if org else "福島県", "prefecture": "福島県",
                "published_at": "" if is_result else pub, "deadline": "",
                "result_date": pub if is_result else "", "result_url": url if is_result else "",
                "project_code": f"FUKUSHIMA-{'R-' if is_result else ''}{slug}", "awardee": "",
                "awardee_checked": "1" if is_result else "",
                "amount": "", "url": url, "source": "FUKUSHIMA",
                "source_category": "入札結果" if is_result else "",
                "summary": "", "detail": "", "tags": ",".join(generate_tags(title, org)),
            })
    logger.info(f"福島県: {len(results)}件取得")
    return results


async def scrape_fukushima() -> List[Dict]:
    """福島県公式サイトの入札・契約情報（全部局集約）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_fukushima_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"福島県スクレイパー例外: {e}")
        return []


def fetch_fukushima_detail(url: str) -> Optional[Dict]:
    """福島県 入札・公募 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"福島県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="tmp_contents") or soup.find(id="page-content") or soup.find("main") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 鳥取県（新規）。とりネット（ASP.NET）「その他入札情報」9511.htm。
# <div class="SubBox">日付</div><div class="Title"><a href="/N.htm">案件名</a>。
# 調達公告・委託・プロポが中心。
# ---------------------------------------------------------------------------
_TOTTORI_BASE = "https://www.pref.tottori.lg.jp"
_TOTTORI_LIST = _TOTTORI_BASE + "/9511.htm"
_TOTTORI_ROW = re.compile(
    r'<div class="SubBox">\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*</div>\s*'
    r'<div class="Title">\s*<a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', re.S)


def _scrape_tottori_sync() -> List[Dict]:
    import urllib.request
    import ssl as _ssl
    from urllib.parse import urljoin
    import html as _html
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    try:
        html_doc = op.open(_TOTTORI_LIST, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"鳥取県一覧取得失敗: {e}")
        return []
    results, seen = [], set()
    for m in _TOTTORI_ROW.finditer(html_doc):
        y, mo, d, href, raw = m.group(1), m.group(2), m.group(3), m.group(4), _html.unescape(m.group(5)).strip()
        url = urljoin(_TOTTORI_BASE, href)
        if url in seen:
            continue
        seen.add(url)
        title = re.sub(r"^(?:【[^】]*】)+", "", raw).strip()
        if len(title) < 5:
            continue
        pub = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        is_result = bool(re.search(r"結果|選考|落札者|開札結果|選定しました|決定しました", raw))
        cat = "プロポーザル" if re.search(r"プロポ|提案競技|企画競争|企画提案|公募型", raw) else "入札"
        slug = re.sub(r"[^0-9]", "", href.rsplit("/", 1)[-1]) or str(len(seen))
        results.append({
            "title": title, "category": cat, "organization": "鳥取県", "prefecture": "鳥取県",
            "published_at": "" if is_result else pub, "deadline": "",
            "result_date": pub if is_result else "", "result_url": url if is_result else "",
            "project_code": f"TOTTORI-{'R-' if is_result else ''}{slug}", "awardee": "",
            "awardee_checked": "1" if is_result else "",
            "amount": "", "url": url, "source": "TOTTORI",
            "source_category": "入札結果" if is_result else "",
            "summary": "", "detail": "", "tags": ",".join(generate_tags(title)),
        })
    logger.info(f"鳥取県: {len(results)}件取得")
    return results


async def scrape_tottori() -> List[Dict]:
    """鳥取県公式サイト（とりネット）のその他入札情報を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_tottori_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"鳥取県スクレイパー例外: {e}")
        return []


def fetch_tottori_detail(url: str) -> Optional[Dict]:
    """鳥取県 入札・公募 個別ページの本文を取得する。"""
    import urllib.request
    import ssl as _ssl
    from urllib.parse import urljoin
    try:
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"鳥取県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="tmp_contents") or soup.find(id="ContentsBox") or soup.find("main") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 群馬県（新規）。県公式「入札／公売／公募」/site/nyuusatsu/ のカテゴリ別一覧
# （大分・広島と同じ article_date/article_title CMS）。プロポ・委託・物品・落札情報。
# ---------------------------------------------------------------------------
_GUNMA_BASE = "https://www.pref.gunma.jp"
_GUNMA_CATEGORIES = [
    ("/site/nyuusatsu/list135-769.html", "土木・建築・設備", False),
    ("/site/nyuusatsu/list135-770.html", "調査・委託", False),
    ("/site/nyuusatsu/list135-771.html", "物品等", False),
    ("/site/nyuusatsu/list135-773.html", "プロポーザル等", False),
    ("/site/nyuusatsu/list135-772.html", "県有地等売払い・貸付", False),
    ("/site/nyuusatsu/list135-774.html", "落札情報等", True),
]
_GUNMA_ROW = re.compile(
    r'<span class="article_date">(\d{4})年(\d{1,2})月(\d{1,2})日</span>\s*'
    r'<span class="article_title">\s*<a href="([^"]+)">([^<]+)</a>', re.S)


def _scrape_gunma_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    import html as _html
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    results, seen = [], set()
    for path, cat_label, force_result in _GUNMA_CATEGORIES:
        try:
            html_doc = op.open(_GUNMA_BASE + path, timeout=40).read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            logger.error(f"群馬県一覧取得失敗（{cat_label}）: {e}")
            continue
        for m in _GUNMA_ROW.finditer(html_doc):
            y, mo, d, href, raw = m.group(1), m.group(2), m.group(3), m.group(4), _html.unescape(m.group(5)).strip()
            url = urljoin(_GUNMA_BASE, href)
            if url in seen:
                continue
            seen.add(url)
            title = re.sub(r"^(?:【[^】]*】)+", "", raw).strip()
            if len(title) < 5:
                continue
            pub = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
            is_result = force_result or bool(re.search(r"結果|落札者|開札結果|選定しました|決定しました", raw))
            cat = "プロポーザル" if re.search(r"プロポ|提案競技|企画競争|企画提案|公募型", raw + cat_label) else "入札"
            slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1].replace(".html", "")).strip("-") or str(len(seen))
            results.append({
                "title": title, "category": cat, "organization": "群馬県", "prefecture": "群馬県",
                "published_at": "" if is_result else pub, "deadline": "",
                "result_date": pub if is_result else "", "result_url": url if is_result else "",
                "project_code": f"GUNMA-{'R-' if is_result else ''}{slug}", "awardee": "",
                "awardee_checked": "1" if is_result else "",
                "amount": "", "url": url, "source": "GUNMA",
                "source_category": (cat_label + " 結果") if (is_result and cat_label != "落札情報等") else cat_label,
                "summary": "", "detail": "", "tags": ",".join(generate_tags(title)),
            })
    logger.info(f"群馬県: {len(results)}件取得")
    return results


async def scrape_gunma() -> List[Dict]:
    """群馬県公式サイトの入札・公募情報（プロポ・委託・物品等）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_gunma_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"群馬県スクレイパー例外: {e}")
        return []


def fetch_gunma_detail(url: str) -> Optional[Dict]:
    """群馬県 入札・公募 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"群馬県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="tmp_contents") or soup.find(id="page-content") or soup.find("main") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 埼玉県（新規）。県公式「新着情報一覧 - 各種手続・入札」が全部局横断の集約フィード。
# <li>M月D日<a href="/path.html">案件名</a>。年は無いので現在年から推定。企画提案競技中心。
# ---------------------------------------------------------------------------
_SAITAMA_BASE = "https://www.pref.saitama.lg.jp"
_SAITAMA_LIST = _SAITAMA_BASE + "/kense/tetsuzuki/shinchaku/index.html"
_SAITAMA_ROW = re.compile(r'<li>\s*(\d{1,2})月(\d{1,2})日\s*<a href="([^"]+)">([^<]+)</a>', re.S)


def _scrape_saitama_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    from datetime import date
    import html as _html
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    try:
        html_doc = op.open(_SAITAMA_LIST, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"埼玉県一覧取得失敗: {e}")
        return []
    today = date.today()
    results, seen = [], set()
    for m in _SAITAMA_ROW.finditer(html_doc):
        mo, d, href, raw = int(m.group(1)), int(m.group(2)), m.group(3), _html.unescape(m.group(4)).strip()
        url = urljoin(_SAITAMA_BASE, href)
        if url in seen:
            continue
        seen.add(url)
        title = re.sub(r"^(?:【[^】]*】)+", "", raw).strip()
        if len(title) < 5:
            continue
        try:
            cand = date(today.year, mo, d)
            if (cand - today).days > 30:
                cand = date(today.year - 1, mo, d)
        except ValueError:
            continue
        pub = cand.isoformat()
        is_result = bool(re.search(r"結果|候補者の決定|落札者|開札結果|選定しました|決定しました", raw))
        cat = "プロポーザル" if re.search(r"プロポ|提案競技|企画競争|企画提案|公募型", raw) else "入札"
        slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1].replace(".html", "")).strip("-") or str(len(seen))
        results.append({
            "title": title, "category": cat, "organization": "埼玉県", "prefecture": "埼玉県",
            "published_at": "" if is_result else pub, "deadline": "",
            "result_date": pub if is_result else "", "result_url": url if is_result else "",
            "project_code": f"SAITAMA-{'R-' if is_result else ''}{slug}", "awardee": "",
            "awardee_checked": "1" if is_result else "",
            "amount": "", "url": url, "source": "SAITAMA",
            "source_category": "入札結果" if is_result else "",
            "summary": "", "detail": "", "tags": ",".join(generate_tags(title)),
        })
    logger.info(f"埼玉県: {len(results)}件取得")
    return results


async def scrape_saitama() -> List[Dict]:
    """埼玉県公式サイトの新着情報一覧（各種手続・入札）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_saitama_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"埼玉県スクレイパー例外: {e}")
        return []


def fetch_saitama_detail(url: str) -> Optional[Dict]:
    """埼玉県 入札・公募 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"埼玉県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="tmp_contents") or soup.find(id="page-content") or soup.find("main") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 岩手県（新規）。県公式「入札・公募 新着更新情報一覧」/news/1016275.html が全部局横断
# の集約フィード。<li class="box"><span class="date">令和X年Y月Z日</span>…<span class="newsli">
# <a href>案件名</a>。※日付は令和表記（20\d\d正規表現では拾えない点に注意）。
# ---------------------------------------------------------------------------
_IWATE_BASE = "https://www.pref.iwate.jp"
_IWATE_LIST = _IWATE_BASE + "/news/1016275.html"
_IWATE_ROW = re.compile(
    r'<li class="box">\s*<span class="date">令和(\d+)年(\d{1,2})月(\d{1,2})日</span>'
    r'.*?<span class="newsli">\s*<a href="([^"]+)">([^<]+)</a>', re.S)


def _scrape_iwate_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    import html as _html
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    try:
        html_doc = op.open(_IWATE_LIST, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"岩手県一覧取得失敗: {e}")
        return []
    results, seen = [], set()
    for m in _IWATE_ROW.finditer(html_doc):
        y, mo, d, href, raw = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4), _html.unescape(m.group(5)).strip()
        url = urljoin(_IWATE_LIST, href)
        if url in seen:
            continue
        seen.add(url)
        title = re.sub(r"^(?:【[^】]*】)+", "", raw).strip()
        if len(title) < 5:
            continue
        pub = f"{2018 + y:04d}-{mo:02d}-{d:02d}"
        is_result = bool(re.search(r"審査|選考|結果|落札者|開札結果|決定しました|選定しました", raw))
        cat = "プロポーザル" if re.search(r"プロポ|企画提案|企画競争|提案競技|公募型|コンペ", raw) else "入札"
        slug = re.sub(r"[^0-9]", "", href.rsplit("/", 1)[-1]) or str(len(seen))
        results.append({
            "title": title, "category": cat, "organization": "岩手県", "prefecture": "岩手県",
            "published_at": "" if is_result else pub, "deadline": "",
            "result_date": pub if is_result else "", "result_url": url if is_result else "",
            "project_code": f"IWATE-{'R-' if is_result else ''}{slug}", "awardee": "",
            "awardee_checked": "1" if is_result else "",
            "amount": "", "url": url, "source": "IWATE",
            "source_category": "入札結果" if is_result else "",
            "summary": "", "detail": "", "tags": ",".join(generate_tags(title)),
        })
    logger.info(f"岩手県: {len(results)}件取得")
    return results


async def scrape_iwate() -> List[Dict]:
    """岩手県公式サイトの入札・公募 新着更新情報一覧（全部局集約）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_iwate_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"岩手県スクレイパー例外: {e}")
        return []


def fetch_iwate_detail(url: str) -> Optional[Dict]:
    """岩手県 入札・公募 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"岩手県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="voice") or soup.find("main") or soup.find(id="pagebody") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 香川県（新規）。県公式「ページ 一覧（入札）」cgi。テーブル形式で全部局横断。
# <td>N.</td><td>YYYY年M月D日</td><td><a href>案件名</a></td><td>部局</td>。
# ---------------------------------------------------------------------------
_KAGAWA_BASE = "https://www.pref.kagawa.lg.jp"
_KAGAWA_LIST = _KAGAWA_BASE + "/cgi-bin/page/list.php?tpl_type=2&page_type=5"
_KAGAWA_ROW = re.compile(
    r'<td>\s*\d+\.?\s*</td>\s*<td>\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*</td>\s*'
    r'<td>\s*<a href\s*=\s*"([^"]+)"\s*>([^<]+)</a>\s*</td>\s*<td>([^<]*)</td>', re.S)


def _scrape_kagawa_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    import html as _html
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    try:
        html_doc = op.open(_KAGAWA_LIST, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"香川県一覧取得失敗: {e}")
        return []
    results, seen = [], set()
    for m in _KAGAWA_ROW.finditer(html_doc):
        y, mo, d, href, raw, org = m.group(1), m.group(2), m.group(3), m.group(4), _html.unescape(m.group(5)).strip(), _html.unescape(m.group(6)).strip()
        url = urljoin(_KAGAWA_BASE, href)
        if url in seen:
            continue
        seen.add(url)
        title = re.sub(r"^(?:【[^】]*】)+", "", raw).strip()
        if len(title) < 5:
            continue
        pub = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        is_result = bool(re.search(r"結果|落札者|開札結果|選定しました|決定しました|候補者", raw))
        cat = "プロポーザル" if re.search(r"プロポ|企画提案|企画競争|提案競技|公募型", raw) else "入札"
        slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1].replace(".html", "")).strip("-") or str(len(seen))
        results.append({
            "title": title, "category": cat,
            "organization": f"香川県（{org}）" if org else "香川県", "prefecture": "香川県",
            "published_at": "" if is_result else pub, "deadline": "",
            "result_date": pub if is_result else "", "result_url": url if is_result else "",
            "project_code": f"KAGAWA-{'R-' if is_result else ''}{slug}", "awardee": "",
            "awardee_checked": "1" if is_result else "",
            "amount": "", "url": url, "source": "KAGAWA",
            "source_category": "入札結果" if is_result else "",
            "summary": "", "detail": "", "tags": ",".join(generate_tags(title, org)),
        })
    logger.info(f"香川県: {len(results)}件取得")
    return results


async def scrape_kagawa() -> List[Dict]:
    """香川県公式サイトの入札一覧（全部局横断）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_kagawa_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"香川県スクレイパー例外: {e}")
        return []


def fetch_kagawa_detail(url: str) -> Optional[Dict]:
    """香川県 入札・公募 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"香川県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="tmp_contents") or soup.find(id="main") or soup.find("main") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 青森県（新規）。青森県建設業ポータル配下の Oracle mod_plsql 入札情報検索。
# koji_nyus_sel2(工事)/con_nyus_sel2(委託) に POST。発注者=*******(全)/場所=***/方式=*/
# 評価=* ＋ 公告日は空・入札日を年月指定(なし選択肢が無く年月必須。日=d全部)で結果取得。
# 一覧: 発注者/入札方式/総合評価/実施公告日/入札執行日(全角M月D日)/工事番号/工事名(A link)。
# ★一覧に「年」が無いため会計年度で推定＋入札執行日が今日〜+120日の開札前のみ採用、
#   中止/不調/取止は除外して品質を確保。詳細=NYUS_RESULT1?p_ktuban=。
# ---------------------------------------------------------------------------
_AOMORI_BASE = "http://pub.pref.aomori.lg.jp/pls/doboku/"
_AOMORI_ZEN = str.maketrans("０１２３４５６７８９", "0123456789")
_AOMORI_CANCEL = re.compile(r"中止|不調|取止|取り止|取りやめ|中断|延期")
_AOMORI_ENDPOINTS = [("koji_nyus_sel2", "工事"), ("con_nyus_sel2", "委託")]
_AOMORI_FORWARD_DAYS = 120


def _scrape_aomori_sync() -> List[Dict]:
    import urllib.request
    import ssl as _ssl
    import urllib.parse
    from urllib.parse import urljoin
    from datetime import date, timedelta
    import html as _html
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    today = date.today()
    hi = today + timedelta(days=_AOMORI_FORWARD_DAYS)
    cur_fy = today.year if today.month >= 4 else today.year - 1

    def fy_year(mo):  # 令和年度(4月始まり)で年を推定
        return cur_fy if mo >= 4 else cur_fy + 1

    def post(ep, ny, nm):
        p = {"p_hattyu": "*******", "p_koji_basyo": "***", "p_nyus_hoshiki": "*", "p_hyoka": "*",
             "p_koukoku_year": "", "p_koukoku_month": "", "p_koukoku_day": "d",
             "p_nyus_year": ny, "p_nyus_month": nm, "p_nyus_day": "d"}
        data = urllib.parse.urlencode(p, encoding="cp932").encode()
        req = urllib.request.Request(_AOMORI_BASE + ep, data=data, headers={"User-Agent": "Mozilla/5.0"})
        return op.open(req, timeout=60).read().decode("cp932", "replace")

    results, seen = [], set()
    for ep, cd_label in _AOMORI_ENDPOINTS:
        try:
            # 入札日フィルタは実質効かず全件返る。年月指定でデータを引き出し、client側で絞る。
            html_doc = post(ep, str(cur_fy), f"{today.month:02d}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"青森県取得失敗（{cd_label}）: {e}")
            continue
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html_doc, re.S | re.I):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)
            if len(tds) < 7:
                continue
            c = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", t))).translate(_AOMORI_ZEN).strip() for t in tds]
            dm = re.search(r"(\d{1,2})月(\d{1,2})日", c[4])  # 入札執行日
            kno = re.search(r"p_ktuban=(\d+)", tds[6]) or re.search(r"p_ktuban=(\d+)", tds[5])
            if not dm or not kno:
                continue
            title = c[6].strip()
            if len(title) < 4 or _AOMORI_CANCEL.search(title):
                continue
            mo, d = int(dm.group(1)), int(dm.group(2))
            try:
                bid = date(fy_year(mo), mo, d)
            except ValueError:
                continue
            if not (today <= bid <= hi):  # 開札前かつ近未来のみ（年推定の誤りを抑制）
                continue
            key = kno.group(1)
            if key in seen:
                continue
            seen.add(key)
            org = {"東青": "東青地域", "中南": "中南地域", "三八": "三八地域", "西北": "西北地域",
                   "上北": "上北地域", "下北": "下北地域"}.get(c[0], c[0])
            cat = "プロポーザル" if re.search(r"プロポ|企画提案|企画競争|公募型", title) else "入札"
            results.append({
                "title": title, "category": cat,
                "organization": f"青森県（{org}）" if org else "青森県", "prefecture": "青森県",
                "published_at": "", "deadline": bid.isoformat(),
                "result_date": "", "result_url": "",
                "project_code": f"AOMORI-{key}", "awardee": "", "awardee_checked": "",
                "amount": "", "url": urljoin(_AOMORI_BASE, f"NYUS_RESULT1?p_ktuban={key}"),
                "source": "AOMORI", "source_category": cd_label,
                "summary": "", "detail": "", "tags": ",".join(generate_tags(title)),
            })
    logger.info(f"青森県: {len(results)}件取得")
    return results


async def scrape_aomori() -> List[Dict]:
    """青森県 建設工事・委託の入札情報（現在開札前）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_aomori_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"青森県スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 山形県（新規）。県公式 入札情報の「公募型プロポーザル」「業務委託の入札(県庁)」等。
# <li><a href="/path.html">【部局】案件名（…提出期限：令和X年Y月Z日…）</a>。
# 日付はタイトル内に埋め込み（提出期限）。部局は【】。
# ---------------------------------------------------------------------------
_YAMAGATA_BASE = "https://www.pref.yamagata.jp"
_YAMAGATA_LISTS = [
    ("/kensei/nyuusatsujouhou/nyuusatsujouhou/proposal/index.html", "プロポーザル"),
    ("/kensei/nyuusatsujouhou/nyuusatsujouhou/gyoumuitaku/itkpref/index.html", "業務委託"),
]
_YAMAGATA_ROW = re.compile(r'<li>\s*<a href="([^"]+\.html)">\s*(【[^】]+】[^<]+)</a>', re.S)


def _scrape_yamagata_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    import html as _html
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    results, seen = [], set()
    for path, cat_label in _YAMAGATA_LISTS:
        try:
            html_doc = op.open(_YAMAGATA_BASE + path, timeout=40).read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            logger.error(f"山形県一覧取得失敗（{cat_label}）: {e}")
            continue
        for m in _YAMAGATA_ROW.finditer(html_doc):
            href, raw = m.group(1), _html.unescape(m.group(2)).strip()
            url = urljoin(_YAMAGATA_BASE, href)
            if url in seen:
                continue
            seen.add(url)
            org_m = re.match(r"【([^】]+)】", raw)
            org = org_m.group(1) if org_m else ""
            body = re.sub(r"^【[^】]*】", "", raw).strip()
            # タイトル末尾の（…提出期限：…）等の注記から締切(最後の令和日付)を取り、本文は括弧前まで
            deadline = ""
            dts = re.findall(r"令和(\d+)年(\d{1,2})月(\d{1,2})日", raw)
            if dts:
                y, mo, d = dts[-1]
                deadline = f"{2018 + int(y):04d}-{int(mo):02d}-{int(d):02d}"
            title = re.sub(r"（[^（）]*(?:期限|まで|締切|日時)[^（）]*）\s*$", "", body).strip()
            title = re.sub(r"\s*（$", "", title).strip()
            if len(title) < 5:
                continue
            is_result = bool(re.search(r"結果|選定しました|審査結果|落札者|決定しました", raw))
            cat = "プロポーザル" if re.search(r"プロポ|企画提案|企画競争|公募型", raw + cat_label) else "入札"
            slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1].replace(".html", "")).strip("-") or str(len(seen))
            results.append({
                "title": title, "category": cat,
                "organization": f"山形県（{org}）" if org else "山形県", "prefecture": "山形県",
                "published_at": "", "deadline": "" if is_result else deadline,
                "result_date": deadline if is_result else "", "result_url": url if is_result else "",
                "project_code": f"YAMAGATA-{'R-' if is_result else ''}{slug}", "awardee": "",
                "awardee_checked": "1" if is_result else "",
                "amount": "", "url": url, "source": "YAMAGATA",
                "source_category": cat_label + (" 結果" if is_result else ""),
                "summary": "", "detail": "", "tags": ",".join(generate_tags(title, org)),
            })
    logger.info(f"山形県: {len(results)}件取得")
    return results


async def scrape_yamagata() -> List[Dict]:
    """山形県公式サイトの入札情報（公募型プロポ・業務委託）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_yamagata_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"山形県スクレイパー例外: {e}")
        return []


def fetch_yamagata_detail(url: str) -> Optional[Dict]:
    """山形県 入札・公募 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"山形県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    # 更新日/掲載日はページのヘッダ/メタ領域にあり本文スコープ外のことがあるため、
    # decompose前の全ページテキストから抽出する
    published_at = _labeled_date_iso(soup.get_text(" ", strip=True), ["更新日", "掲載日", "公告日"])
    main = soup.find(id="tmp_contents") or soup.find(id="tmp_read_contents") or soup.find("main") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments,
            "published_at": published_at}


# ---------------------------------------------------------------------------
# 山口県（新規）。県公式「入札情報」/life/sub/5/index-2.html（大分等と同じ
# article_date/article_title CMS）。総務系の公募型プロポ・指定管理者選定等。
# ※工事・物品・委託の多くは電子入札掲示板(JS描画)側で静的取得外。
# ---------------------------------------------------------------------------
_YAMAGUCHI_LIST = "https://www.pref.yamaguchi.lg.jp/life/sub/5/index-2.html"
_YAMAGUCHI_ROW = re.compile(
    r'<span class="article_date">(\d{4})年(\d{1,2})月(\d{1,2})日更新</span>\s*'
    r'<span class="article_title">\s*<a href="([^"]+)">([^<]+)</a>', re.S)


def _scrape_yamaguchi_sync() -> List[Dict]:
    import urllib.request
    import ssl as _ssl
    from urllib.parse import urljoin
    import html as _html
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    try:
        html_doc = op.open(_YAMAGUCHI_LIST, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"山口県一覧取得失敗: {e}")
        return []
    results, seen = [], set()
    for m in _YAMAGUCHI_ROW.finditer(html_doc):
        y, mo, d, href, raw = m.group(1), m.group(2), m.group(3), m.group(4), _html.unescape(m.group(5)).strip()
        url = urljoin(_YAMAGUCHI_LIST, href)
        if url in seen:
            continue
        seen.add(url)
        title = re.sub(r"^(?:【[^】]*】)+", "", raw).strip()
        if len(title) < 5:
            continue
        pub = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        is_result = bool(re.search(r"結果|優先交渉権者|選定について|落札者|決定について", raw))
        cat = "プロポーザル" if re.search(r"プロポ|企画提案|企画競争|公募型|指定管理", raw) else "入札"
        slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1].replace(".html", "")).strip("-") or str(len(seen))
        results.append({
            "title": title, "category": cat, "organization": "山口県", "prefecture": "山口県",
            "published_at": "" if is_result else pub, "deadline": "",
            "result_date": pub if is_result else "", "result_url": url if is_result else "",
            "project_code": f"YAMAGUCHI-{'R-' if is_result else ''}{slug}", "awardee": "",
            "awardee_checked": "1" if is_result else "",
            "amount": "", "url": url, "source": "YAMAGUCHI",
            "source_category": "入札結果" if is_result else "",
            "summary": "", "detail": "", "tags": ",".join(generate_tags(title)),
        })
    logger.info(f"山口県: {len(results)}件取得")
    return results


async def scrape_yamaguchi() -> List[Dict]:
    """山口県公式サイトの入札情報（公募型プロポ等）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_yamaguchi_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"山口県スクレイパー例外: {e}")
        return []


def fetch_yamaguchi_detail(url: str) -> Optional[Dict]:
    """山口県 入札・公募 個別ページの本文を取得する。"""
    import urllib.request
    import ssl as _ssl
    from urllib.parse import urljoin
    try:
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"山口県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="tmp_contents") or soup.find(id="main") or soup.find("main") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 宮城県 物品等電子調達システム（efftis 公開案件検索）
#   建設工事はOTeaフレームセット(JSメニュー)で別途Playwright要。物品・役務は
#   /04900/public/pubOrderSearch.do がraw HTTPで検索可能（cp932・Struts型）。
#   methodName=execSearch で年度(nend)・状況(eqvSts=10受付中)を指定→10件/頁。
#   結果フォームを継承して methodName=execApplyListRowLength + inputListRowLength=100
#   で全件を1頁に展開する。詳細はexecOrderDetail(POST)でURL化不可のため一覧完結。
# ---------------------------------------------------------------------------
_MIYAGI_HOST = "https://miyagi.efftis.jp"
_MIYAGI_INIT = _MIYAGI_HOST + "/04900/public/pubOrderSearch.do?methodName=initDisplay"


def _scrape_miyagi_sync() -> List[Dict]:
    import urllib.request
    import urllib.parse
    import ssl as _ssl
    import http.cookiejar
    import html as _html
    from datetime import date as _date

    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    except Exception:  # noqa: BLE001
        pass
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def _get(u):
        return op.open(u, timeout=40).read().decode("cp932", "replace")

    def _post(u, data):
        enc = urllib.parse.urlencode(data, encoding="cp932").encode("ascii")
        req = urllib.request.Request(u, data=enc)
        req.add_header("User-Agent", "Mozilla/5.0")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        return op.open(req, timeout=60).read().decode("cp932", "replace")

    def _inputs(h):
        d = {}
        for m in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*>', h, re.I):
            v = re.search(r'value="([^"]*)"', m.group(0))
            d[m.group(1)] = _html.unescape(v.group(1)) if v else ""
        return d

    def _action(h):
        m = re.search(r'<form[^>]*action="([^"]+)"', h, re.I)
        return _MIYAGI_HOST + m.group(1) if m else _MIYAGI_INIT

    try:
        init = _get(_MIYAGI_INIT)
        data = _inputs(init)
        # 現在＋翌年度の受付中を対象（年度替わりの取りこぼし防止）
        nend = _date.today().year if _date.today().month >= 4 else _date.today().year - 1
        data["methodName"] = "execSearch"
        data["nend"] = str(nend)
        data["eqvSts"] = "10"  # 受付中（開札前）
        res = _post(_action(init), data)
        # 表示件数100で全件を1頁に展開
        d2 = _inputs(res)
        d2["methodName"] = "execApplyListRowLength"
        d2["inputListRowLength"] = "100"
        res2 = _post(_action(res), d2)
        if len(re.findall(r"execOrderDetail'", res2)) >= len(re.findall(r"execOrderDetail'", res)):
            res = res2
    except Exception as e:  # noqa: BLE001
        logger.error(f"宮城県一覧取得失敗: {e}")
        return []

    results, seen = [], set()
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", res, re.I | re.S):
        if "execOrderDetail" not in tr:
            continue
        am = re.search(r"execOrderDetail'[^>]*>(.*?)</a>", tr, re.S)
        title = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", am.group(1)))).strip() if am else ""
        if len(title) < 3:
            continue
        plain = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", tr)))
        num_m = re.search(r"(\d{15})", plain)
        num = num_m.group(1) if num_m else ""
        dt = re.search(r"(\d{4})/(\d{2})/(\d{2})", plain)
        opendate = f"{dt.group(1)}-{dt.group(2)}-{dt.group(3)}" if dt else ""
        org_m = re.search(r"OpenBelongViewForPub\([^>]*>(.*?)</a>", tr, re.S)
        org = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", org_m.group(1)))).strip() if org_m else ""
        kind_m = re.search(r"([物役])/", plain)
        kind = "物品" if (kind_m and kind_m.group(1) == "物") else ("役務" if kind_m else "")
        key = num or re.sub(r"[^A-Za-z0-9]+", "", title)[:16]
        if key in seen:
            continue
        seen.add(key)
        url = _MIYAGI_HOST + "/04900/public/pubOrderSearch.do?c=" + key
        results.append({
            "title": title, "category": "入札",
            "organization": f"宮城県（{org}）" if org else "宮城県", "prefecture": "宮城県",
            "published_at": "", "deadline": opendate, "close_date": opendate,
            "result_date": "", "result_url": "",
            "project_code": num, "awardee": "", "awardee_checked": "",
            "amount": "", "url": url, "source": "MIYAGI",
            "source_category": kind,
            "summary": "", "detail": "", "tags": ",".join(generate_tags(title, org)),
        })
    logger.info(f"宮城県: {len(results)}件取得")
    return results


async def scrape_miyagi() -> List[Dict]:
    """宮城県 物品等電子調達システムの公開案件（受付中）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_miyagi_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"宮城県スクレイパー例外: {e}")
        return []


def fetch_kochi_detail(url: str) -> Optional[Dict]:
    """高知県 入札公告 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"高知県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="main_body") or soup.find("main") or soup.find(id="tmp_contents") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


def fetch_ehime_detail(url: str) -> Optional[Dict]:
    """愛媛県 入札公告 個別ページの本文を取得する（PDFはスキップ）。"""
    import urllib.request
    from urllib.parse import urljoin
    if re.search(r"\.pdf($|\?)", url, re.I):
        return None
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"愛媛県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="main_body") or soup.find("main") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


def fetch_okayama_detail(url: str) -> Optional[Dict]:
    """岡山県 入札公告 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"岡山県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="main_body") or soup.find("main") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


def fetch_hiroshima_detail(url: str) -> Optional[Dict]:
    """広島県 入札公告 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"広島県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="main_body") or soup.find("main") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


def fetch_wakayama_detail(url: str) -> Optional[Dict]:
    """和歌山県 入札・公募 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"和歌山県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="tmp_contents") or soup.find("main") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


def fetch_shiga_detail(url: str) -> Optional[Dict]:
    """滋賀県 入札公告 個別ページの本文を取得する（締切等を補完）。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"滋賀県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="tmp_contents") or soup.find("main") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# 兵庫県 開札結果（応札結果）。入札公告と同じ縦型テーブル構造。落札者・落札金額は
# 各結果詳細ページ（部署ごとに書式バラバラ・PDF等）にあり定型抽出できないため、
# 結果レコードとして案件名・発注機関・開札日(入札日)・公式結果ページリンクを収録する。
_HYOGO_RESULT_CATEGORIES = [
    ("/bid/bid_res_02.html", "委託・役務"),
    ("/bid/bid_res_03.html", "工事・設計"),
]


def _scrape_hyogo_results_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    results, seen = [], set()
    for path, cat_label in _HYOGO_RESULT_CATEGORIES:
        try:
            html_doc = get(_HYOGO_BASE + path)
        except Exception as e:  # noqa: BLE001
            logger.error(f"兵庫県開札結果取得失敗（{cat_label}）: {e}")
            continue
        for tb in re.findall(r"<table[^>]*>(.*?)</table>", html_doc, re.S | re.I):
            if "名称" not in tb or not re.search(r'href="/[a-z]', tb):
                continue
            fields, link = {}, ""
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tb, re.S | re.I):
                cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
                if len(cells) < 2:
                    continue
                label = re.sub(r"<[^>]+>", "", cells[0]).strip()
                val = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", cells[1])).strip()
                fields[label] = val
                if label == "名称":
                    lm = re.search(r'href="([^"]+)"', cells[1])
                    if lm:
                        link = lm.group(1)
            title = re.sub(r"(?:の)?(?:入札|開札|審査)?結果$", "", fields.get("名称", "")).strip()
            if not title or not link:
                continue
            url = urljoin(_HYOGO_BASE, link)
            if url in seen:
                continue
            seen.add(url)
            method = fields.get("入札方法", "")
            cat = "プロポーザル" if re.search(r"プロポーザル|企画提案|公募型", method + title) else "入札"
            org = ("兵庫県 " + fields.get("発注機関", "")).strip()
            rdate = _hyogo_date_iso(fields.get("入札日", "") or fields.get("開札日", ""))
            slug = re.sub(r"[^A-Za-z0-9]+", "-", link.rsplit("/", 1)[-1]).strip("-") or str(len(seen))
            results.append({
                "title":           title,
                "category":        cat,
                "organization":    org,
                "prefecture":      "兵庫県",
                "published_at":    "",
                "deadline":        "",
                "result_date":     rdate,
                "result_url":      url,
                "project_code":    f"HYOGO-R-{slug}",
                "awardee":         "",   # 結果ページの書式が不定型のため落札者はリンク先で確認
                "awardee_checked": "1",  # 自動抽出しない（監視終了）
                "amount":          "",
                "url":             url,
                "source":          "HYOGO",
                "source_category": (fields.get("種別", cat_label) + " 開札結果").strip(),
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title, fields.get("発注機関", ""))),
            })
    logger.info(f"兵庫県 開札結果: {len(results)}件取得")
    return results


async def scrape_hyogo_results() -> List[Dict]:
    """兵庫県の開札結果（応札結果）を取得する。落札者はリンク先で確認（自動抽出不可）。"""
    try:
        return await asyncio.to_thread(_scrape_hyogo_results_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"兵庫県開札結果スクレイパー例外: {e}")
        return []


def fetch_hyogo_detail(url: str) -> Optional[Dict]:
    """兵庫県 入札公告 個別ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html_doc = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"兵庫県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html_doc, "html.parser")
    main = soup.find(id="tmp_contents") or soup.find("main") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"[（(][^）)]*(?:KB|MB|バイト)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            attachments.append({"name": name, "url": urljoin(url, a["href"]), "kind": "公告文"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments, "published_at": ""}


# ---------------------------------------------------------------------------
# 静岡県（部局別ページ14件。横断一覧は無いため全部局を個別に巡回する。建設工事は対象外）
# ---------------------------------------------------------------------------
_SHIZUOKA_BASE = "https://www.pref.shizuoka.jp"
_SHIZUOKA_DEPT_PATHS = [
    "nyusatsuchiji", "nyusatsukikikanri", "1079568", "1072932", "nyusatsukeieikanri",
    "1082273", "nyusatsukeizaisangyou", "nyusatsukenkou", "nyusatsukurashi",
    "nyusatsusports", "1047032", "1081677", "koukoku", "1077988",
]


def _scrape_shizuoka_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    results = []
    seen = set()
    for dept in _SHIZUOKA_DEPT_PATHS:
        url = f"{_SHIZUOKA_BASE}/kensei/nyusatsukobai/{dept}/index.html"
        try:
            html = get(url)
        except Exception as e:  # noqa: BLE001
            logger.error(f"静岡県一覧取得失敗（{dept}）: {e}")
            continue
        m = re.search(r'<ul class="listlink clearfix">(.*?)</ul>', html, re.S)
        list_html = m.group(1) if m else ""
        links = re.findall(r'<li>\s*<a href="([^"]+)">([^<]+)</a>', list_html)
        for href, title in links:
            full = urljoin(url, href)
            if full in seen:
                continue
            seen.add(full)
            title = title.strip()
            cat = "プロポーザル" if re.search(r"プロポーザル|企画提案|公募", title) else "入札"
            slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-")
            results.append({
                "title":           title,
                "category":        cat,
                "organization":    "静岡県",
                "prefecture":      "静岡県",
                "published_at":    "",  # 一覧に日付が無いため詳細ページの更新日で補完
                "deadline":        "",
                "result_date":     "",
                "result_url":      "",
                "project_code":    f"SHIZUOKA-{slug}",
                "awardee":         "",
                "url":             full,
                "source":          "SHIZUOKA",
                "amount":          "",
                "source_category": "",
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title)),
            })
        import time as _time
        _time.sleep(0.6)
    logger.info(f"静岡県: {len(results)}件取得")
    return results


async def scrape_shizuoka() -> List[Dict]:
    """静岡県公式サイト 入札・業務委託・プロポーザル等（部局別14ページ）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_shizuoka_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"静岡県スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 汎用 SuperCALS「入札情報公開システム」スクレイパー（ebidPPIPublish / EjPSJ01）。
# 千葉・福井・石川・長野で確立したPOST連鎖を1関数に集約し、新規のSuperCALS県は
# 設定(dict)を渡すだけで追加できるようにする。既存4県は稼働実績尊重で当面据え置き、
# 新規（静岡ほか）からこの汎用関数を使う。共通詳細パーサ _parse_chiba_cals_detail を利用。
#
# cfg キー:
#   ej(str), kikan(str), pref(str), source(str),
#   choutatsu: [(cd, label), ...]         調達区分（00工事/01測量等）
#   window_days(int)                       公告日サーバ側ウィンドウ（BidStDate/BidEnDate）
#   max_detail(int), sleep(float)          詳細取得の上限・間隔（gov負荷配慮）
#   extra: [(k, v), ...]                   findListの追加必須フィールド（福井のEbidCD等）
#   status_open: tuple|None, status_col: int   一覧状態列で現在公告中に絞る（長野型）
#   title_from: 'list'|'detail', title_col: int
# ---------------------------------------------------------------------------
_SUPERCALS_TITLE_PREFIX = re.compile(r"^（入札番号[:：][^）]*）\s*")


def _cals_list_date(s: str) -> str:
    """SuperCALS一覧セルの日付を ISO(YYYY-MM-DD) へ。令和08/07/02・R08-07-14 両対応。"""
    if not s:
        return ""
    m = re.search(r"(?:令和|Ｒ|R)\s*0?(\d{1,2})[/\-年.]\s*0?(\d{1,2})[/\-月.]\s*0?(\d{1,2})", s)
    if m:
        return f"{2018 + int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def _scrape_supercals_ppi(cfg: Dict) -> List[Dict]:
    import hashlib
    import html as _html
    import time as _time
    import urllib.request
    import urllib.parse
    import http.cookiejar
    from datetime import date, timedelta

    ej, kikan = cfg["ej"], cfg["kikan"]
    pref, source = cfg["pref"], cfg["source"]
    today = date.today()
    lo = today - timedelta(days=cfg.get("window_days", 90))
    # 一部県(鹿児島)は検索日付範囲が「入札予定日」を絞るため、BidEnDateを未来へ延ばす必要がある
    hi = today + timedelta(days=cfg.get("bid_end_days_ahead", 0))
    bs, be = lo.strftime("%Y/%m/%d"), hi.strftime("%Y/%m/%d")
    fy = today.year if today.month >= 4 else today.year - 1
    status_open = cfg.get("status_open")
    status_col = cfg.get("status_col", 4)
    title_from = cfg.get("title_from", "list")
    title_col = cfg.get("title_col", 2)
    sleep = cfg.get("sleep", 0.25)
    budget = cfg.get("max_detail", 250)

    jar = http.cookiejar.CookieJar()
    handlers = [urllib.request.HTTPCookieProcessor(jar)]
    if cfg.get("ssl_seclevel1"):
        # 一部SuperCALS(鹿児島等)は弱いDH鍵で標準TLSが弾かれるためcipherを緩める
        import ssl as _ssl
        _ctx = _ssl.create_default_context()
        try:
            _ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        except _ssl.SSLError:
            pass
        handlers.append(urllib.request.HTTPSHandler(context=_ctx))
    op = urllib.request.build_opener(*handlers)
    op.addheaders = [("User-Agent", "Mozilla/5.0"), ("Referer", ej)]

    def post(pairs):
        return op.open(ej, data=urllib.parse.urlencode(pairs).encode(), timeout=60).read().decode("cp932", "replace")

    def get(url):
        return op.open(url, timeout=60).read().decode("cp932", "replace")

    results: List[Dict] = []
    try:
        get(f"{ej}?KikanNO={kikan}")
        post([("ejParameterID", "StartPage"), ("KikanNO", kikan)])
    except Exception as e:  # noqa: BLE001
        logger.error(f"{pref}(SuperCALS)セッション確立失敗: {e}")
        return results

    for cd, cd_label in cfg["choutatsu"]:
        try:
            post([("ejParameterID", "EjPSJ01"), ("ejProcessName", "start")])
            get(ej + "?ejParameterID=EjPSJ01&ejShousaiDispFlag=false&ejProcessName=getCondPage")
            base = [
                ("Nendo", str(fy)), ("KikanNO", kikan), ("ChoutatsuCD", cd),
                ("BukyokuNO", ""), ("KoujiSyubetu", ""), ("BidStDate", bs), ("BidEnDate", be),
                ("kkselect", "AND"), ("mojisel1", ""), ("mojisel2", ""),
                ("chiiki_dataList", ""), ("chiikisentaku", ""), ("getStpos", "0"), ("AllhitSize", "0"),
                ("ejMaxDisplayRowCount", "700"), ("ejDisplaySort", "030006"), ("ejSortSequence", "desc"),
                ("ejParameterID", "EjPSJ01"), ("ejProcessName", "findList"), ("ejShousaiDispFlag", "false"),
            ] + cfg.get("extra", [])
            lst = post(base)
        except Exception as e:  # noqa: BLE001
            logger.error(f"{pref}(SuperCALS)検索失敗（{cd_label}）: {e}")
            continue

        fv = re.search(r'ejFindVersion"\s*value="(\d+)"', lst)
        find_version = fv.group(1) if fv else ""
        if not find_version:
            continue

        for tr in re.findall(r"<TR[^>]*>(.*?)</TR>", lst, re.S | re.I):
            if "openYotei" not in tr:
                continue
            idxm = re.search(r"openYotei\('?(\d+)'?\)", tr)
            if not idxm:
                continue
            idx = idxm.group(1)
            tds = re.findall(r"<TD[^>]*>(.*?)</TD>", tr, re.S)
            cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", c))).replace("\xa0", " ").strip() for c in tds]
            if status_open:
                st = cells[status_col] if len(cells) > status_col else ""
                if not any(st.startswith(s) for s in status_open):
                    continue
            list_title = cells[title_col] if len(cells) > title_col else ""

            def _cell(i):
                return cells[i] if (i is not None and len(cells) > i) else ""
            list_pub = _cals_list_date(_cell(cfg.get("list_pub_col")))
            list_deadline = _cals_list_date(_cell(cfg.get("list_deadline_col")))

            # open_only: 詳細取得の「前」に一覧の締切列で絞り、開札済みは詳細を叩かない。
            # open_deadline_col が締切相当（入札予定日）。値が過去なら現在公告中でないため除外。
            # 空欄は「入札予定日が一覧に未掲載＝公告中」の可能性があり残す（詳細で日付補完）。
            open_col = cfg.get("open_deadline_col", cfg.get("list_deadline_col"))
            list_open = _cals_list_date(_cell(open_col))
            if cfg.get("open_only") and list_open and list_open < today.isoformat():
                continue

            # 一覧に日付列があれば詳細取得を省く（大量案件のホスト負荷・CI時間を抑制）
            has_list_dates = (cfg.get("list_pub_col") is not None) or (cfg.get("list_deadline_col") is not None)
            need_detail = (title_from == "detail") or (not has_list_dates)

            org, published, deadline, amount, gyoshu, dtitle = pref, list_pub, list_deadline, "", cd_label, ""
            if need_detail and budget > 0:
                try:
                    dv = post([
                        ("ejParameterID", "EjPSJ01"), ("ejProcessName", "getDetailPage"),
                        ("ejCategoryName", "display"), ("ejKeyNo", idx), ("ejFindVersion", find_version),
                        ("ejStartPosition", "0"), ("ejMaxDisplayRowCount", "700"), ("ejShousaiDispFlag", "false"),
                    ])
                    budget -= 1
                    info = _parse_chiba_cals_detail(dv)
                    org = info["org"] or pref
                    published = info["published_at"] or list_pub
                    deadline = info["deadline"] or list_deadline
                    amount = info["amount"]
                    gyoshu = info["gyoshu"] or cd_label
                    dtitle = info["title"]
                    _time.sleep(sleep)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"{pref}(SuperCALS)詳細取得失敗（idx={idx}）: {e}")
            elif title_from == "detail":
                continue  # 詳細必須なのに予算切れ→スキップ

            raw_title = dtitle if (title_from == "detail" and dtitle) else list_title
            title = _SUPERCALS_TITLE_PREFIX.sub("", re.sub(r"\s*※\s*添付有\s*$", "", raw_title)).strip()
            # 一部県(栃木)は一覧の案件名セル先頭に案件番号(例 208-157009 )が付く→cfgで除去
            _ts = cfg.get("title_strip")
            if _ts:
                title = re.sub(_ts, "", title).strip()
            if not title:
                continue
            slug = hashlib.md5((title + (published or idx)).encode("utf-8")).hexdigest()[:12]
            results.append({
                "title":           title,
                "category":        "入札",
                "organization":    org,
                "prefecture":      pref,
                "published_at":    published,
                "deadline":        deadline,
                "result_date":     "",
                "result_url":      "",
                "project_code":    f"{source}-CALS-{slug}",
                "awardee":         "",
                "url":             f"{ej}?KikanNO={kikan}#{slug}",
                "source":          source,
                "amount":          amount,
                "source_category": gyoshu,
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title, org)),
            })
    logger.info(f"{pref} 建設(SuperCALS): {len(results)}件取得")
    return results


_SHIZUOKA_CALS_CFG = {
    "ej": "https://www.ppi.cals-shiz.jp/ebidPPIPublish/EjPPIj",
    "kikan": "2200000",  # 静岡県（静岡市2210000・浜松市2220200等は別）
    "pref": "静岡県", "source": "SHIZUOKA",
    "choutatsu": [("00", "工事"), ("01", "測量・コンサル")],
    "window_days": 90, "max_detail": 200, "sleep": 0.25,
    "title_from": "list", "title_col": 2,
}


async def scrape_shizuoka_cals() -> List[Dict]:
    """静岡県 建設工事・測量コンサル（静岡県共同利用入札情報システム SuperCALS）を取得する。

    既存の静岡スクレイパーは部局ページ（委託・プロポ中心）で建設工事が欠落していたため補完。
    汎用 _scrape_supercals_ppi を利用（新規SuperCALS県はこの方式で設定追加のみで対応可）。
    """
    try:
        return await asyncio.to_thread(_scrape_supercals_ppi, _SHIZUOKA_CALS_CFG)
    except Exception as e:  # noqa: BLE001
        logger.error(f"静岡県建設スクレイパー例外: {e}")
        return []


# 宮崎県（新規）。宮崎県電子入札情報公開システム（SuperCALS）。県本体 KikanNO=4500000。
# 一覧に案件名(列1)・公告日(列5)・入札予定日(列7)が揃うため詳細取得不要。
# EjPSJ01(入札予定/公告)は現在システム掲載中の案件を大量に返すので、open_onlyで
# 入札予定日が未到来の現在公告中のみ採用（土日は0件になり得る＝正常。平日CIで収集）。
_MIYAZAKI_CALS_CFG = {
    "ej": "https://www.e-nyusatsu-joho.pref.miyazaki.lg.jp/ebidPPIPublish/EjPPIj",
    "kikan": "4500000",
    "pref": "宮崎県", "source": "MIYAZAKI",
    "choutatsu": [("00", "工事"), ("01", "測量・コンサル")],
    "window_days": 30, "max_detail": 0,
    "title_from": "list", "title_col": 1,
    "list_pub_col": 5, "list_deadline_col": 7,
    "open_only": True,
}


async def scrape_miyazaki() -> List[Dict]:
    """宮崎県 電子入札情報公開システム（SuperCALS）の現在公告中の入札を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_supercals_ppi, _MIYAZAKI_CALS_CFG)
    except Exception as e:  # noqa: BLE001
        logger.error(f"宮崎県スクレイパー例外: {e}")
        return []


# 鹿児島県（新規）。かごしま県市町村共同 電子入札(SuperCALS)。県本体 KikanNO=46000。
# ★弱DH鍵のため ssl_seclevel1 必須。運用8:30-20:00(平日)。列: 案件名=col2 / 入札予定日=col1。
# 公告日窓が効かない(公告は年度初で入札予定は数ヶ月後)ため広い窓＋open_only(入札予定日≥今日)。
_KAGOSHIMA_CALS_CFG = {
    "ej": "https://www.kagoshima-nyusatsu.jp/ebidPPIPublish/EjPPIj",
    "kikan": "46000",
    "pref": "鹿児島県", "source": "KAGOSHIMA",
    "choutatsu": [("00", "工事"), ("01", "測量・コンサル")],
    "window_days": 30, "bid_end_days_ahead": 210, "max_detail": 0,
    "title_col": 2, "list_deadline_col": 1, "open_deadline_col": 1,
    "open_only": True, "ssl_seclevel1": True,
}


async def scrape_kagoshima() -> List[Dict]:
    """鹿児島県 電子入札システム（SuperCALS）の現在公告中の入札を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_supercals_ppi, _KAGOSHIMA_CALS_CFG)
    except Exception as e:  # noqa: BLE001
        logger.error(f"鹿児島県スクレイパー例外: {e}")
        return []


# 新潟県 建設工事（既存の新潟スクレイパーは委託・公募中心で建設工事が欠落）。
# ep-bis.pref.niigata.jp SuperCALS。県本体 KikanNO=1500000。案件名=列2。
# 一覧の入札予定日(列7)が過去の開札済みは open_only で除外。開札前(列7空=公告中)は
# 一覧に日付が無いため詳細を取得して公告日・締切を補完（開札前のみなので件数は少ない）。
_NIIGATA_CALS_CFG = {
    "ej": "https://www.ep-bis.pref.niigata.jp/ebidPPIPublish/EjPPIj",
    "kikan": "1500000",
    "pref": "新潟県", "source": "NIIGATA",
    "choutatsu": [("00", "工事"), ("01", "測量・コンサル")],
    "window_days": 30, "max_detail": 150, "sleep": 0.25,
    "title_from": "list", "title_col": 2,
    "open_deadline_col": 7, "open_only": True,
}


async def scrape_niigata_cals() -> List[Dict]:
    """新潟県 建設工事・測量コンサル（新潟県共同利用電子入札 SuperCALS）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_supercals_ppi, _NIIGATA_CALS_CFG)
    except Exception as e:  # noqa: BLE001
        logger.error(f"新潟県建設スクレイパー例外: {e}")
        return []


# 栃木県 建設工事・測量コンサル（とちぎ電子入札 SuperCALS・ep-bis.supercals.jp）。
# 県本体 KikanNO=0900000。一覧は1日付列(col1=入札予定日)のみ・案件名(col2)先頭に案件番号
# (例 208-157009 )が付く→title_strip で除去。BidStDate/BidEnDateは入札予定日を絞る(鹿児島型)
# ため bid_end_days_ahead で未来へ延ばし、open_deadline_col=1 で入札予定日>=今日を現在公告中に。
_TOCHIGI_CALS_CFG = {
    "ej": "https://www.ep-bis.supercals.jp/ebidPPIPublish/EjPPIj",
    "kikan": "0900000",
    "pref": "栃木県", "source": "TOCHIGI_EBID",
    "choutatsu": [("00", "工事"), ("01", "測量・コンサル")],
    "window_days": 7, "bid_end_days_ahead": 150, "max_detail": 0,
    "title_col": 2, "title_strip": r"^\d{2,4}-\d{5,7}\s*",
    "list_deadline_col": 1, "open_deadline_col": 1, "open_only": True,
}


async def scrape_tochigi_cals() -> List[Dict]:
    """栃木県 建設工事・測量コンサル（とちぎ電子入札 SuperCALS）の現在公告中を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_supercals_ppi, _TOCHIGI_CALS_CFG)
    except Exception as e:  # noqa: BLE001
        logger.error(f"栃木県建設スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# efftis（Struts型 PPUBC）汎用ソルバー。富山・奈良・秋田・宮城等の入札情報サービス。
# フロー: GET PPUBC00100?kikanno=（セッション）→ GET PPUBC00100!link?screenId=
#   PPUBC00400&chotatsu_kbn=00&organizationNumber=（発注情報の検索フォーム）→
#   POST PPUBC00400（kensakuJoken.*＋method:search）でステータス別サマリ →
#   POST PPUBC00400!link（seniKbn=1入札参加申請受付/2入札待ち）で実案件一覧。
# 1案件=3TR: row1[契約番号,発注機関,件名(link),業種,方式,提出開始日,開く] /
#   row2[提出締切日単独] / row3[場所,入札手段(電子/紙),公告日,開札予定日]。
# 弱いDH鍵のため set_ciphers('DEFAULT@SECLEVEL=1') が必須。
# ---------------------------------------------------------------------------
def _efftis_wareki(s: str) -> str:
    m = re.search(r"令和(\d+)年(\d{1,2})月(\d{1,2})日", s or "")
    if m:
        return f"{2018 + int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def _scrape_efftis_struts(cfg: Dict) -> List[Dict]:
    import hashlib
    import ssl as _ssl
    import urllib.request
    import urllib.parse
    import http.cookiejar
    import html as _html
    from datetime import date, timedelta

    base = cfg["base"]
    org = cfg["org"]
    kikanno = cfg.get("kikanno", org)
    pref, source = cfg["pref"], cfg["source"]
    today = date.today()
    lo = today - timedelta(days=cfg.get("window_days", 60))
    fy = str(today.year if today.month >= 4 else today.year - 1)
    seni_open = cfg.get("seni_open", ["1", "2"])
    open_only = cfg.get("open_only", True)

    ctx = _ssl.create_default_context()
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")  # efftisは弱いDH鍵→SECLEVEL下げ必須
    except _ssl.SSLError:
        pass
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar), urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [("User-Agent", "Mozilla/5.0"), ("Referer", base)]

    def get(u):
        return op.open(u, timeout=45).read().decode("utf-8", "replace")

    def post(u, pairs):
        data = urllib.parse.urlencode(pairs, encoding="utf-8").encode()
        req = urllib.request.Request(u, data=data, headers={"Referer": base})
        return op.open(req, timeout=60).read().decode("utf-8", "replace")

    def harvest(html_doc, action):
        fm = re.search(r'<form[^>]*action="\./' + action + r'"[^>]*>(.*?)</form>', html_doc, re.S)
        fm = fm.group(1) if fm else html_doc
        p = {}
        for m in re.findall(r"<input[^>]+>", fm):
            nm = re.search(r'name="([^"]*)"', m)
            tym = re.search(r'type="([^"]*)"', m)
            ty = tym.group(1) if tym else "text"
            vl = re.search(r'value="([^"]*)"', m)
            if nm and ty in ("text", "hidden"):
                p[nm.group(1)] = vl.group(1) if vl else ""
        for nm, blk in re.findall(r'<select[^>]*name="([^"]*)"[^>]*>(.*?)</select>', fm, re.S):
            sel = re.search(r'<option[^>]*value="([^"]*)"[^>]*selected', blk) or re.search(r'<option[^>]*value="([^"]*)"', blk)
            p[nm] = sel.group(1) if sel else ""
        return p, fm

    def cells(tr):
        return [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", t))).strip()
                for t in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]

    results: List[Dict] = []
    try:
        get(f"{base}PPUBC00100?kikanno={kikanno}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"{pref}(efftis)セッション確立失敗: {e}")
        return results

    for kbn, screen, label in cfg["choutatsu"]:
        try:
            h = get(f"{base}PPUBC00100!link?screenId={screen}&chotatsu_kbn={kbn}&organizationNumber={org}")
            params, fm = harvest(h, screen)
            params["kensakuJoken.selNendo"] = fy
            # 富山型は公告日range(textフィールド)で絞る。奈良型(direct_list)はこのtextフィールドが
            # 検索を破壊する(0件化)ため設定しない。年度selectのみで現年度を出す。
            if not cfg.get("direct_list"):
                params.update({
                    "kensakuJoken.textKoukokuFromYear": str(lo.year),
                    "kensakuJoken.textKoukokuFromMonth": str(lo.month),
                    "kensakuJoken.textKoukokuFromDay": str(lo.day),
                    "kensakuJoken.textKoukokuToYear": str(today.year),
                    "kensakuJoken.textKoukokuToMonth": str(today.month),
                    "kensakuJoken.textKoukokuToDay": str(today.day),
                })
            shudan = re.findall(r'name="kensakuJoken.nyusatsuShudanList"[^>]*value="([^"]*)"', fm)
            search_pairs = [(k, v) for k, v in params.items()] + \
                [("kensakuJoken.nyusatsuShudanList", s) for s in shudan] + [("method:search", "検索")]
            summary = post(f"{base}{screen}", search_pairs)
        except Exception as e:  # noqa: BLE001
            logger.error(f"{pref}(efftis)検索失敗（{label}）: {e}")
            continue

        def parse_list(lst, label):
            pend = None
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", lst, re.S | re.I):
                anc = re.search(r"link\('(\d+)',\s*'(\d+)',\s*'([^']+)'\)[^>]*>\s*([^<]+?)\s*</a>", tr)
                c = cells(tr)
                if anc:
                    pend = {
                        "keiyakuNo": anc.group(3),
                        "title": re.sub(r"\s+", " ", _html.unescape(anc.group(4))).strip(),
                        "org": c[1] if len(c) > 1 else pref,
                        "gyoshu": c[3] if len(c) > 3 else label,
                        "published": "", "deadline": "", "_done": False,
                    }
                elif pend is not None and not pend["_done"] and any(re.search(r"電子|郵便|紙", x) and len(x) <= 6 for x in c):
                    # row3の入札手段セル(電子/紙/電子・紙)を探し、その後ろ2セル=公告日・開札予定日。
                    # 富山型[場所,電子,公告日,開札日]と奈良型[電子,公告日,開札日]の両対応。
                    hi = next(i for i, x in enumerate(c) if re.search(r"電子|郵便|紙", x) and len(x) <= 6)
                    pend["published"] = _efftis_wareki(c[hi + 1]) if hi + 1 < len(c) else ""
                    pend["deadline"] = _efftis_wareki(c[hi + 2]) if hi + 2 < len(c) else ""
                    pend["_done"] = True
                    if open_only and pend["deadline"] and pend["deadline"] < today.isoformat():
                        pend = None
                        continue
                    title = pend["title"]
                    if not title:
                        pend = None
                        continue
                    slug = hashlib.md5((source + pend["keiyakuNo"]).encode("utf-8")).hexdigest()[:12]
                    cat = "プロポーザル" if re.search(r"プロポ|企画提案|企画競争|公募型", title) else "入札"
                    results.append({
                        "title": title, "category": cat,
                        "organization": pend["org"] or pref, "prefecture": pref,
                        "published_at": pend["published"], "deadline": pend["deadline"],
                        "result_date": "", "result_url": "",
                        "project_code": f"{source}-EFF-{pend['keiyakuNo']}", "awardee": "",
                        "amount": "",
                        "url": f"{base}PPUBC00100?kikanno={kikanno}#{slug}",
                        "source": source, "source_category": pend["gyoshu"] or label,
                        "summary": "", "detail": "",
                        "tags": ",".join(generate_tags(title, pend["org"] or pref)),
                    })
                    pend = None

        if cfg.get("direct_list"):
            # 奈良型: method:search の応答が案件一覧そのもの（seniKbnドリルダウン無し）
            parse_list(summary, label)
            continue
        # 富山型: 検索応答はステータス別サマリ → seniKbnで実案件一覧へドリルダウン
        p2, _ = harvest(summary, screen)
        for seni in seni_open:
            try:
                p2b = dict(p2)
                p2b["seniKbn"] = seni
                lst = post(f"{base}{screen}!link", [(k, v) for k, v in p2b.items()])
            except Exception as e:  # noqa: BLE001
                logger.error(f"{pref}(efftis)一覧取得失敗（{label}/seni{seni}）: {e}")
                continue
            parse_list(lst, label)
    logger.info(f"{pref} 建設(efftis): {len(results)}件取得")
    return results


_TOYAMA_EFF_CFG = {
    "base": "https://toyama.efftis.jp/ebid01/PPI/Public/",
    "org": "160008", "kikanno": "160008",
    "pref": "富山県", "source": "TOYAMA",
    "choutatsu": [("00", "PPUBC00400", "工事"), ("01", "PPUBC00400", "測量・コンサル"),
                  ("11", "PPUBC00410", "物品・役務")],
    "seni_open": ["1", "2"], "window_days": 90, "open_only": True,
}


async def scrape_toyama_cals() -> List[Dict]:
    """富山県 建設工事等（とやま電子入札共同システム efftis Struts型）の現在公告中を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_efftis_struts, _TOYAMA_EFF_CFG)
    except Exception as e:  # noqa: BLE001
        logger.error(f"富山県建設スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 京都府・香川県 入札情報公開システム（efftis旧JSP系 = NEC「入札情報公開システム」
# OME egov PPI・frameset＋Velocity(.vm)）。過去「未攻略・多段トークン」とされた難物を
# raw HTTP で攻略（2026-07-27 実測攻略）。両県とも同一ベンダー・攻略手順は共通:
#   1. GET 入口(frameset) でセッションCookie(＋香川はURLパスの ;jsessionid=)を確立
#   2. GET 検索start.vm → auto-submit する隠しフォーム(omeProcessName=start)を模倣し
#      POST {画面}Start.do → 検索条件フォーム(conditionform)が返る
#   3. conditionform の全hidden/選択値をharvestし条件を上書きして
#      POST {画面}GetList.do(omeProcessName=findList) → 案件一覧
# 重要な差異:
#   ・.vm(frameset/menu)は Shift_JIS、.do(検索/一覧)は UTF-8（charset自動判定で吸収）
#   ・京都: kyoto.efftis.jp/26000/CALS/PPI_P/ 。工事・測量コンサルのみ(物品は別系統無し)。
#     PiCtBaFi02(全案件詳細検索)を使用。発注機関=京都府に限定。omeMaxDisplayRowCount=1000で
#     年度内全件を1頁取得可。24時間稼働(土0-7時除く)。列: [No,調達機関,案件名,場所,種別,
#     入札方式,資料配布(期間),申請受付(期間),詳細]。日付は令和表記。
#   ・香川: dennyu.pref.kagawa.lg.jp/PPI_P/ 。県+市町の共同システム(発注機関で香川県のみ抽出)。
#     PiCtCrFi01(工事r1=00/コンサルr1=01)・PiCtCrFi02(物品)。運用8:00-22:00(時間外はトップへ
#     リダイレクト→0件で正常終了)。結果>100件は中間頁を挟むため getListPage で
#     omeStartPosition を進めてページング(omePageDirection=absolute)。列: [No,公告日,発注機関,
#     発注組織,案件名,入札方式]。一覧に締切列が無いためdeadlineは空・公告日(西暦)をpublished_atに。
# 運用時間外・メンテ時はトップページ("運用時間"/Facebookトラッカ)へ飛ぶため is_top で検知し[]。
# ---------------------------------------------------------------------------
def _efftis_ppi_opener():
    import ssl as _ssl
    import urllib.request
    import http.cookiejar
    ctx = _ssl.create_default_context()
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")  # efftisは弱DH鍵→SECLEVEL下げ
    except _ssl.SSLError:
        pass
    # 証明書チェーン/ホスト名不備に強くする（efftis系は中間証明書欠落の前例あり）
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    return op, jar


def _ppi_decode(raw: bytes) -> str:
    """PPI_P応答をcharset自動判定でデコード（.vm=Shift_JIS / .do=UTF-8）。"""
    m = re.search(rb'charset=["\']?([\w\-]+)', raw[:2000], re.I)
    enc = (m.group(1).decode() if m else "cp932").lower()
    if enc in ("shift_jis", "shift-jis", "sjis", "x-sjis"):
        enc = "cp932"
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("cp932", "replace")


def _ppi_harvest(body: str, formname: str) -> Dict[str, str]:
    """指定フォームのhidden/text/select(選択値)を回収して dict 化。"""
    m = re.search(r'<form[^>]*name="' + formname + r'"[^>]*>(.*?)</form>', body, re.S | re.I)
    inner = m.group(1) if m else body
    pairs: Dict[str, str] = {}
    for inp in re.findall(r"<input[^>]+>", inner):
        nm = re.search(r'name="([^"]*)"', inp)
        if not nm:
            continue
        tym = re.search(r'type=["\']?(\w+)', inp)
        ty = tym.group(1).lower() if tym else "text"
        vlm = re.search(r'value="([^"]*)"', inp)
        val = vlm.group(1) if vlm else ""
        if ty in ("hidden", "text"):
            pairs[nm.group(1)] = val
        elif ty in ("radio", "checkbox") and "checked" in inp:
            pairs[nm.group(1)] = val
    for sel in re.finditer(r'<select[^>]*name="([^"]*)"[^>]*>(.*?)</select>', inner, re.S | re.I):
        selm = (re.search(r'<option[^>]*value="([^"]*)"[^>]*selected', sel.group(2))
                or re.search(r'<option[^>]*value="([^"]*)"', sel.group(2)))
        pairs[sel.group(1)] = selm.group(1) if selm else ""
    return pairs


def _ppi_rows(html_doc: str) -> List[List[str]]:
    """一覧テーブルの getdetail(N) を含む行のセル配列を返す。"""
    import html as _html
    out: List[List[str]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html_doc, re.S | re.I):
        if "getdetail(" not in tr:
            continue
        cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", c))).replace("\xa0", " ").strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        out.append(cells)
    return out


def _ppi_is_top(body: str) -> bool:
    """運用時間外/セッション切れでトップページへ飛ばされたかを判定。"""
    return ("trackFacebook" in body) or ("運用時間" in body) or ("フレームの見れる" in body)


_WAREKI_DATE = re.compile(r"令和(\d+)年\s*(\d{1,2})月\s*(\d{1,2})日")
_SEIREKI_DATE = re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日")


def _ppi_dates(text: str) -> List[str]:
    """令和/西暦表記の日付をすべてISO(YYYY-MM-DD)で抽出（出現順）。"""
    out: List[str] = []
    for m in re.finditer(r"令和(\d+)年\s*(\d{1,2})月\s*(\d{1,2})日|(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", text or ""):
        if m.group(1):
            y = 2018 + int(m.group(1))
            mo, d = int(m.group(2)), int(m.group(3))
        else:
            y, mo, d = int(m.group(4)), int(m.group(5)), int(m.group(6))
        out.append(f"{y:04d}-{mo:02d}-{d:02d}")
    return out


def _scrape_kyoto_ebid_sync() -> List[Dict]:
    import hashlib
    import urllib.request
    import urllib.parse
    from datetime import date

    ROOT = "https://kyoto.efftis.jp/26000/CALS/PPI_P/"
    today = date.today().isoformat()
    op, _jar = _efftis_ppi_opener()

    def get(u):
        return _ppi_decode(op.open(u, timeout=60).read())

    def post(u, pairs):
        data = urllib.parse.urlencode(pairs, encoding="utf-8").encode()
        return _ppi_decode(op.open(urllib.request.Request(u, data=data), timeout=90).read())

    results: List[Dict] = []
    try:
        get(ROOT)  # セッションCookie確立
        form = post(ROOT + "PiCtBaFi02Start.do", [
            ("omeProcessName", "start"),
            ("omeParameterGroupID", "jp.co.nec.ome.egov.ppi.pi.ct.ba.fi.PiCtBaFi02E01.Start"),
        ])
    except Exception as e:  # noqa: BLE001
        logger.error(f"京都府電子入札セッション確立失敗: {e}")
        return results
    if _ppi_is_top(form):
        logger.info("京都府電子入札: 運用時間外/リダイレクト（0件）")
        return results

    base = _ppi_harvest(form, "conditionform")
    # 年度・発注機関(京都府)・供給種別(工事/コンサル)コードはフォームの hidden から動的取得
    # （"2026PPIORG001" 等の年度接頭辞はFYで変わるためハードコードしない）。
    fy = (base.get("pPI_BNSYEAR") or "").strip()
    org_kyoto = (base.get("pPI_ORGCDvalues", "").split("|") or [""])[0]  # 先頭=京都府
    sply_codes = [s for s in base.get("pPI_SPLYCDvalues", "").split("|") if s]  # 工事,コンサル
    if not sply_codes:
        sply_codes = [""]

    for sply in sply_codes:
        d = dict(base)
        d["omeProcessName"] = "findList"
        d["r1"] = "v2"                       # v2=入札公告・入札情報（v3=結果）
        d["pPI_ORGCD"] = org_kyoto           # 京都府のみ（市町村を除外）
        d["pPI_SPLYCD"] = sply
        if fy:
            d["pPI_BNSYEAR"] = fy
        d["omeMaxDisplayRowCount"] = "1000"  # 年度内全件を1頁取得
        try:
            lst = post(ROOT + "PiCtBaFi02GetList.do", list(d.items()))
        except Exception as e:  # noqa: BLE001
            logger.error(f"京都府電子入札検索失敗（sply={sply}）: {e}")
            continue
        if _ppi_is_top(lst):
            continue
        for cells in _ppi_rows(lst):
            if len(cells) < 6:
                continue
            title = cells[2].strip()
            if not title:
                continue
            org = cells[1].strip() or "京都府"
            gyoshu = cells[4].strip()
            # c[6]=資料配布(期間) / c[7]=申請受付(期間)。公告日≈資料配布開始、
            # 締切≈申請受付終了。両セルの全日付から最早=公告、最遅=締切とする。
            dc = _ppi_dates(cells[6] if len(cells) > 6 else "")
            de = _ppi_dates(cells[7] if len(cells) > 7 else "")
            all_d = sorted(dc + de)
            published = (dc or all_d or [""])[0]
            deadline = (all_d or [""])[-1]
            # 現在公告中のみ（最終アクション日が未到来）。日付不明はキープ。
            if deadline and deadline < today:
                continue
            cat = "プロポーザル" if re.search(r"プロポ|企画提案|企画競争|公募型", title) else "入札"
            slug = hashlib.md5(("KYOTO_EBID" + title + published).encode("utf-8")).hexdigest()[:12]
            results.append({
                "title": title,
                "category": cat,
                "organization": org,
                "prefecture": "京都府",
                "published_at": published,
                "deadline": deadline,
                "result_date": "",
                "result_url": "",
                "project_code": f"KYOTO_EBID-{slug}",
                "awardee": "",
                "url": f"{ROOT}#{slug}",
                "source": "KYOTO_EBID",
                "amount": "",
                "source_category": gyoshu,
                "summary": "",
                "detail": "",
                "tags": ",".join(generate_tags(title, org)),
            })
    logger.info(f"京都府 電子入札(PPI): {len(results)}件取得")
    return results


async def scrape_kyoto_ebid() -> List[Dict]:
    """京都府 入札情報公開システム（工事・測量コンサル）の現在公告中の入札を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_kyoto_ebid_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"京都府電子入札スクレイパー例外: {e}")
        return []


# 香川県 入札方式(=入札後審査型一般競争 等)。プロポ判定に使用。
_KAGAWA_METHODS = {
    "一般競争入札", "公募型指名競争入札", "指名競争入札", "随意契約",
    "公募型プロポーザル", "指名型プロポーザル", "入札後審査型一般競争",
}


def _scrape_kagawa_ebid_sync() -> List[Dict]:
    import hashlib
    import time as _time
    import urllib.request
    import urllib.parse
    from datetime import date, timedelta
    from urllib.parse import urljoin

    ROOT = "https://dennyu.pref.kagawa.lg.jp/PPI_P/"
    today = date.today().isoformat()
    window_lo = (date.today() - timedelta(days=120)).isoformat()  # 古すぎる公告は除外
    op, _jar = _efftis_ppi_opener()

    # frameset入口で ;jsessionid= を確立（WebLogic系はパスにjsessionidを埋める）
    try:
        entry = _ppi_decode(op.open(ROOT, timeout=60).read())
    except Exception as e:  # noqa: BLE001
        logger.error(f"香川県電子入札セッション確立失敗: {e}")
        return []
    jm = re.search(r";jsessionid=([^\s'\"?]+)", entry)
    jsid = jm.group(1) if jm else None

    def _u(u):
        u = urljoin(ROOT, u)
        if jsid and ";jsessionid=" not in u:
            if "?" in u:
                b, q = u.split("?", 1)
                u = f"{b};jsessionid={jsid}?{q}"
            else:
                u = f"{u};jsessionid={jsid}"
        return u

    def get(u):
        return _ppi_decode(op.open(_u(u), timeout=60).read())

    def post(u, pairs):
        data = urllib.parse.urlencode(pairs, encoding="utf-8").encode()
        return _ppi_decode(op.open(urllib.request.Request(_u(u), data=data), timeout=90).read())

    # メニュー導線を辿ってセッション状態を初期化（香川はこれが無いと検索が0件化する）
    try:
        get("pages/Menu/ppi_p_menu.vm?procKbn=")
        get("pages/Menu/ppi_p_main.vm?procKbn=")
    except Exception as e:  # noqa: BLE001
        logger.error(f"香川県電子入札メニュー初期化失敗: {e}")
        return []

    results: List[Dict] = []

    def run_category(screen, gid_stub, start_vm, r1, label):
        """1カテゴリ(工事/コンサル/物品)を検索しページングして香川県分を収集。"""
        try:
            get(f"pages/PPI_P/{screen}/{start_vm}")
            form = post(f"/PPI_P/{screen}Start.do", [
                ("omeProcessName", "start"),
                ("omeParameterGroupID", f"jp.co.nec.ome.egov.ppi.pi.ct.cr.fi.{gid_stub}E01.Start"),
            ])
        except Exception as e:  # noqa: BLE001
            logger.error(f"香川県電子入札({label})開始失敗: {e}")
            return
        if _ppi_is_top(form):
            logger.info(f"香川県電子入札({label}): 運用時間外/リダイレクト")
            return
        base = _ppi_harvest(form, "conditionform")
        base["omeProcessName"] = "findList"
        base["omeParameterGroupID"] = f"jp.co.nec.ome.egov.ppi.pi.ct.cr.fi.{gid_stub}E01.FindList"
        base["ppikikanno"] = ""      # 全機関（香川県抽出は行の発注機関列で行う）
        base["condition6"] = ""
        base["condition7"] = ""
        base["omeMaxDisplayRowCount"] = "100"
        base["display"] = "100"
        if r1 is not None:
            base["r1"] = r1
        try:
            res1 = post(f"/PPI_P/{screen}GetList.do", list(base.items()))
        except Exception as e:  # noqa: BLE001
            logger.error(f"香川県電子入札({label})検索失敗: {e}")
            return
        if _ppi_is_top(res1):
            logger.info(f"香川県電子入札({label}): 運用時間外/リダイレクト")
            return
        if "該当のデータは存在しません" in res1 and "omeFindVersion" not in res1:
            return
        fvm = re.search(r'name="omeFindVersion"\s+value="([^"]*)"', res1)
        if not fvm:
            logger.info(f"香川県電子入札({label}): omeFindVersion取得失敗（0件扱い）")
            return
        fv = fvm.group(1)
        gid_get = f"jp.co.nec.ome.egov.ppi.pi.ct.cr.fi.{gid_stub}E01.GetList"

        pos = 0
        for _pg in range(25):  # 安全上限（100件/頁）
            try:
                lst = post(f"/PPI_P/{screen}GetList.do", [
                    ("omeProcessName", "getListPage"),
                    ("omeParameterGroupID", gid_get),
                    ("omeFindVersion", fv), ("omeKeyNo", ""), ("listjudgeflag", "1"),
                    ("omePageDirection", "absolute"),
                    ("omeMaxDisplayRowCount", "100"),
                    ("omeStartPosition", str(pos)), ("omeEndPosition", str(pos + 99)),
                ])
            except Exception as e:  # noqa: BLE001
                logger.error(f"香川県電子入札({label})ページ取得失敗（pos={pos}）: {e}")
                break
            if _ppi_is_top(lst):
                break
            rows = _ppi_rows(lst)
            if not rows:
                break
            for cells in rows:
                # 発注機関列に"香川県"が単独で入る行のみ採用（市町・企業団を除外）
                if "香川県" not in cells:
                    continue
                # 案件名 = 日付でも方式でも機関でもない最長セル
                cand = [c for c in cells
                        if c and c != "香川県"
                        and not _SEIREKI_DATE.search(c) and not _WAREKI_DATE.search(c)
                        and c not in _KAGAWA_METHODS
                        and not re.fullmatch(r"\d+", c)]
                if not cand:
                    continue
                title = max(cand, key=len).strip()
                if len(title) < 4:
                    continue
                dates = _ppi_dates(" ".join(cells))
                published = dates[0] if dates else ""
                if published and published < window_lo:
                    continue  # 古すぎる公告は除外
                method = next((c for c in cells if c in _KAGAWA_METHODS), "")
                cat = "プロポーザル" if ("プロポ" in method or re.search(r"プロポ|企画提案|企画競争|公募型", title)) else "入札"
                slug = hashlib.md5(("KAGAWA_EBID" + title + published).encode("utf-8")).hexdigest()[:12]
                results.append({
                    "title": title,
                    "category": cat,
                    "organization": "香川県",
                    "prefecture": "香川県",
                    "published_at": published,
                    "deadline": "",
                    "result_date": "",
                    "result_url": "",
                    "project_code": f"KAGAWA_EBID-{slug}",
                    "awardee": "",
                    "url": f"{ROOT}#{slug}",
                    "source": "KAGAWA_EBID",
                    "amount": "",
                    "source_category": label,
                    "summary": "",
                    "detail": "",
                    "tags": ",".join(generate_tags(title, "香川県")),
                })
            if len(rows) < 100:
                break
            pos += 100
            _time.sleep(0.2)

    # 工事 / 測量コンサル / 物品等
    run_category("PiCtCrFi01", "PiCtCrFi01", "PiCtCrFi01start.vm", "00", "建設工事")
    run_category("PiCtCrFi01", "PiCtCrFi01", "PiCtCrFi01start.vm", "01", "測量・コンサル")
    run_category("PiCtCrFi02", "PiCtCrFi02", "PiCtCrFi02start.vm", None, "物品等")
    logger.info(f"香川県 電子入札(PPI): {len(results)}件取得")
    return results


async def scrape_kagawa_ebid() -> List[Dict]:
    """香川県 入札情報公開システム（工事・測量コンサル・物品等）の現在公告中の入札を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_kagawa_ebid_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"香川県電子入札スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 滋賀県 物品・役務電子調達システム（efftis eps/public 公開入札案件検索）
#   工事は別系統（静的 SHIGA で委託等を収集済）。ここでは物品・役務の公告を追加。
#   GET pubGroupTop.do?methodName=execOrderSearch → PubBiddingSearchBean と共に
#   物品/役務の入札公告一覧（10件/頁）が返る。フォームを継承して
#   methodName=execApplyListRowLengthNoCheckForPub + inputListRowLength=100 で
#   全件を1頁展開し、開札予定日が未到来（現在公告中）のみ採用する。cp932/Struts。
#   1行の列: [No, 発注区分/入札方式, 調達番号+案件名, 状況, 発注機関, 公告日+開札予定日時]。
# ---------------------------------------------------------------------------
_SHIGA_EPS_HOST = "https://shiga.efftis.jp"
_SHIGA_EPS_TOP = _SHIGA_EPS_HOST + "/25000/eps/public/pubGroupTop.do?methodName=execOrderSearch&autonomyCd=25000"


def _scrape_shiga_ebid_sync() -> List[Dict]:
    import hashlib
    import urllib.request
    import urllib.parse
    import ssl as _ssl
    import http.cookiejar
    import html as _html
    from datetime import date as _date

    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    except Exception:  # noqa: BLE001
        pass
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def _get(u):
        return op.open(u, timeout=40).read().decode("cp932", "replace")

    def _post(u, data):
        enc = urllib.parse.urlencode(data, encoding="cp932").encode("ascii")
        req = urllib.request.Request(u, data=enc)
        req.add_header("User-Agent", "Mozilla/5.0")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        return op.open(req, timeout=60).read().decode("cp932", "replace")

    def _fields(body):
        m = re.search(r'<form[^>]*name="PubBiddingSearchBean"[^>]*>(.*?)</form>', body, re.S | re.I)
        block = m.group(1) if m else body
        d = {}
        for inp in re.finditer(r'<input\b[^>]*>', block, re.I):
            nm = re.search(r'name="([^"]*)"', inp.group(0))
            vl = re.search(r'value="([^"]*)"', inp.group(0))
            ty = re.search(r'type="([^"]*)"', inp.group(0))
            if nm and (not ty or ty.group(1).lower() in ("hidden", "text")):
                d[nm.group(1)] = _html.unescape(vl.group(1)) if vl else ""
        for sm in re.finditer(r'<select[^>]*name="([^"]*)"[^>]*>(.*?)</select>', block, re.S | re.I):
            sel = (re.search(r'<option[^>]*value="([^"]*)"[^>]*selected', sm.group(2))
                   or re.search(r'<option[^>]*value="([^"]*)"', sm.group(2)))
            d[sm.group(1)] = sel.group(1) if sel else ""
        return d

    def _action(body):
        m = re.search(r'<form[^>]*name="PubBiddingSearchBean"[^>]*action="([^"]*)"', body, re.S | re.I)
        # jsessionid はCookieでも維持されるがパスにも残すと確実
        return _SHIGA_EPS_HOST + m.group(1) if m else _SHIGA_EPS_HOST + "/25000/eps/public/pubBiddingList.do"

    today = _date.today().isoformat()
    results: List[Dict] = []
    try:
        b1 = _get(_SHIGA_EPS_TOP)
        d = _fields(b1)
        act = _action(b1)
        d["methodName"] = "execApplyListRowLengthNoCheckForPub"
        d["inputListRowLength"] = "100"
        d["listRowLength"] = "100"
        body = _post(act, d)
        rows = [tr for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S | re.I) if "openSubWinForPub" in tr]
        if not rows:
            rows = [tr for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", b1, re.S | re.I) if "openSubWinForPub" in tr]
    except Exception as e:  # noqa: BLE001
        logger.error(f"滋賀県物品役務電子調達 取得失敗: {e}")
        return results

    for tr in rows:
        cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 6:
            continue
        method_cell = cells[1]           # 例: 物品調達/ 一般競争入札
        name_cell = cells[2]             # 例: 2607222500000203074 案件名...
        org = cells[4].strip() or "滋賀県"
        date_cell = cells[5]             # 公告日 + 開札予定日時
        onm = re.search(r"orderNum=(\d+)", tr)
        order_num = onm.group(1) if onm else ""
        title = re.sub(r"^\d{10,}\s*", "", name_cell).strip()
        if not title:
            continue
        dates = _ppi_dates(date_cell)
        published = dates[0] if dates else ""
        deadline = dates[1] if len(dates) > 1 else ""
        # 現在公告中のみ（開札予定日が未到来）。日付不明はキープ。
        if deadline and deadline < today:
            continue
        cat = "プロポーザル" if re.search(r"プロポ|企画提案|企画競争|公募型", title + method_cell) else "入札"
        kind = "物品" if "物品" in method_cell else ("役務" if "役務" in method_cell else "物品・役務")
        slug = hashlib.md5(("SHIGA_EBID" + (order_num or title)).encode("utf-8")).hexdigest()[:12]
        results.append({
            "title":           title,
            "category":        cat,
            "organization":    f"滋賀県（{org}）" if org and org != "滋賀県" else "滋賀県",
            "prefecture":      "滋賀県",
            "published_at":    published,
            "deadline":        deadline,
            "result_date":     "",
            "result_url":      "",
            "project_code":    f"SHIGA_EBID-{order_num or slug}",
            "awardee":         "",
            "url":             f"{_SHIGA_EPS_HOST}/25000/eps/public/pubGroupTop.do#{slug}",
            "source":          "SHIGA_EBID",
            "amount":          "",
            "source_category": kind,
            "summary":         "",
            "detail":          "",
            "tags":            ",".join(generate_tags(title, org)),
        })
    logger.info(f"滋賀県 物品役務(電子調達): {len(results)}件取得")
    return results


async def scrape_shiga_ebid() -> List[Dict]:
    """滋賀県 物品・役務電子調達システムの現在公告中の入札を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_shiga_ebid_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"滋賀県物品役務電子調達スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 島根県 電子調達共同利用システム 入札情報サービス（NEC OME PPI /SMN/PPI_P/）
#   京都・香川と同系統（efftis旧JSP / NEC OME PPI）。共通ヘルパを再利用する。
#   入口 choutatsuweb.pref.shimane.lg.jp/portal/ppi → /SMN/PPI_P/ フレームセット。
#   京都型フロー: GET入口→PiCtBaFi02Start.do(start)→conditionform harvest→
#   発注機関=島根県(ORGCD先頭)・供給種別(工事/業務/物品)上書き+omeMaxDisplayRowCount
#   でPiCtBaFi02GetList.do(findList)。一覧列: [No,発注課,案件番号,案件名,場所,工種,
#   入札方式,開札予定日,詳細]。開札予定日>=今日（開札前）を現在公告中として採用。
#   ※日付は「令和 08年06月30日」形式（令和と数字の間に空白）→空白許容パーサで解く。
# ---------------------------------------------------------------------------
_SHIMANE_PPI_ROOT = "https://choutatsuweb.pref.shimane.lg.jp/SMN/PPI_P/"


def _shimane_wareki(s: str) -> str:
    """令和/西暦（空白混じり可）の日付をISO化。例『令和 08年06月30日』。"""
    m = re.search(r"令和\s*(\d+)\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", s or "")
    if m:
        return f"{2018 + int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{4})\s*[年/]\s*(\d{1,2})\s*[月/]\s*(\d{1,2})", s or "")
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def _scrape_shimane_ebid_sync() -> List[Dict]:
    import hashlib
    import urllib.request
    import urllib.parse
    from datetime import date

    ROOT = _SHIMANE_PPI_ROOT
    today = date.today().isoformat()
    op, _jar = _efftis_ppi_opener()

    def get(u):
        return _ppi_decode(op.open(u, timeout=60).read())

    def post(u, pairs):
        data = urllib.parse.urlencode(pairs, encoding="utf-8").encode()
        return _ppi_decode(op.open(urllib.request.Request(u, data=data), timeout=90).read())

    results: List[Dict] = []
    try:
        get(ROOT)  # フレームセットでセッションCookie確立
        get(ROOT + "pages/PPI_P/PiCtBaFi02/PiCtBaFi02start.vm")
        form = post(ROOT + "PiCtBaFi02Start.do", [
            ("omeProcessName", "start"),
            ("omeParameterGroupID", "jp.co.nec.ome.egov.ppi.pi.ct.ba.fi.PiCtBaFi02E01.Start"),
        ])
    except Exception as e:  # noqa: BLE001
        logger.error(f"島根県電子入札セッション確立失敗: {e}")
        return results
    if _ppi_is_top(form):
        logger.info("島根県電子入札: 運用時間外/リダイレクト（0件）")
        return results

    base = _ppi_harvest(form, "conditionform")
    org_shimane = (base.get("pPI_ORGCDvalues", "").split("|") or [""])[0]  # 先頭=島根県
    sply_codes = [s for s in base.get("pPI_SPLYCDvalues", "").split("|") if s]  # 工事/業務/物品
    if not sply_codes:
        sply_codes = [""]

    for sply in sply_codes:
        d = dict(base)
        d["omeProcessName"] = "findList"
        d["r1"] = "v2"                       # v2=入札公告
        d["pPI_ORGCD"] = org_shimane         # 島根県のみ（市町村を除外）
        d["pPI_SPLYCD"] = sply
        d["omeMaxDisplayRowCount"] = "2000"
        try:
            lst = post(ROOT + "PiCtBaFi02GetList.do", list(d.items()))
        except Exception as e:  # noqa: BLE001
            logger.error(f"島根県電子入札検索失敗（sply={sply}）: {e}")
            continue
        if _ppi_is_top(lst):
            continue
        for cells in _ppi_rows(lst):
            if len(cells) < 8:
                continue
            title = cells[3].strip()
            if not title:
                continue
            org = cells[1].strip() or "島根県"
            gyoshu = cells[5].strip()
            method = cells[6].strip()
            deadline = _shimane_wareki(cells[7])  # 開札予定日
            # 現在公告中のみ（開札予定日が未到来）。日付不明はキープ。
            if deadline and deadline < today:
                continue
            cat = "プロポーザル" if re.search(r"プロポ|企画提案|企画競争|公募型", title + method) else "入札"
            slug = hashlib.md5(("SHIMANE_EBID" + (cells[2] or title)).encode("utf-8")).hexdigest()[:12]
            results.append({
                "title":           title,
                "category":        cat,
                "organization":    f"島根県（{org}）" if org and org != "島根県" else "島根県",
                "prefecture":      "島根県",
                "published_at":    "",
                "deadline":        deadline,
                "result_date":     "",
                "result_url":      "",
                "project_code":    f"SHIMANE_EBID-{cells[2] or slug}",
                "awardee":         "",
                "url":             f"{ROOT}#{slug}",
                "source":          "SHIMANE_EBID",
                "amount":          "",
                "source_category": gyoshu,
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title, org)),
            })
    logger.info(f"島根県 電子入札(PPI): {len(results)}件取得")
    return results


async def scrape_shimane_ebid() -> List[Dict]:
    """島根県 電子調達共同利用システム 入札情報サービスの現在公告中の入札を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_shimane_ebid_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"島根県電子入札スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 山形県 入札情報サービス（富士通PPI /PPI/ .shtml、cp932）
#   既存の静的 YAMAGATA はプロポ・委託中心で建設工事が欠落 → 電子入札の
#   入札公告一覧(JS05-01-01)から工事・コンサル・設備の現在公告中を追加する。
#   フロー: GET JS03-01(セッションCookie)→ GET JS05-01-01(検索フォーム)→
#   JS0501Form を harvest し mode=1(検索)・chkB=ON(公開中のみ)・年度・dispCnt=99999
#   でPOST。行に JS05-02.shtml?koujino=<20桁> の詳細リンク。列: [公告日,開札日,
#   案件名,施行地域,発注所属,種別,入札方式,備考]。日付は「2026/ 07/30」形式（空白入り）。
# ---------------------------------------------------------------------------
_YAMAGATA_PPI = "https://ppi.cals.pref.yamagata.jp/PPI/"


def _yamagata_ppi_date(s: str) -> str:
    m = re.search(r"(\d{4})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})", s or "")
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return _shimane_wareki(s)


def _scrape_yamagata_ebid_sync() -> List[Dict]:
    import hashlib
    import urllib.request
    import urllib.parse
    import ssl as _ssl
    import http.cookiejar
    import html as _html
    from datetime import date

    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    except Exception:  # noqa: BLE001
        pass
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(u, ref=None):
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        if ref:
            req.add_header("Referer", ref)
        return op.open(req, timeout=45).read().decode("cp932", "replace")

    def post(u, pairs, ref=None):
        data = urllib.parse.urlencode(pairs, encoding="cp932").encode("ascii")
        req = urllib.request.Request(u, data=data, headers={"User-Agent": "Mozilla/5.0"})
        if ref:
            req.add_header("Referer", ref)
        return op.open(req, timeout=60).read().decode("cp932", "replace")

    def harvest(body, formname):
        m = re.search(r'<form[^>]*name="' + formname + r'"[^>]*>(.*?)</form>', body, re.S | re.I)
        block = m.group(1) if m else ""
        d = {}
        for inp in re.finditer(r'<input\b[^>]*>', block, re.I):
            nm = re.search(r'name="([^"]*)"', inp.group(0))
            ty = re.search(r'type="([^"]*)"', inp.group(0))
            vl = re.search(r'value="([^"]*)"', inp.group(0))
            t = ty.group(1).lower() if ty else "text"
            if nm and t in ("hidden", "text"):
                d[nm.group(1)] = _html.unescape(vl.group(1)) if vl else ""
            elif nm and t in ("radio", "checkbox") and "checked" in inp.group(0).lower():
                d[nm.group(1)] = vl.group(1) if vl else ""
        for sm in re.finditer(r'<select[^>]*name="([^"]*)"[^>]*>(.*?)</select>', block, re.S | re.I):
            sel = (re.search(r'<option[^>]*value="([^"]*)"[^>]*selected', sm.group(2))
                   or re.search(r'<option[^>]*value="([^"]*)"', sm.group(2)))
            d[sm.group(1)] = sel.group(1) if sel else ""
        return d

    today = date.today()
    fy = today.year if today.month >= 4 else today.year - 1
    results: List[Dict] = []
    try:
        get(_YAMAGATA_PPI + "JS03-01.shtml")
        form = get(_YAMAGATA_PPI + "JS05-01-01.shtml", ref=_YAMAGATA_PPI + "JS03-01.shtml")
    except Exception as e:  # noqa: BLE001
        logger.error(f"山形県電子入札セッション確立失敗: {e}")
        return results

    d = harvest(form, "JS0501Form")
    d["mode"] = "1"          # 検索実行
    d["chkB"] = "ON"         # 公開中のみ
    d["koukaichu"] = "on"
    d["nyusatsu_jiki"] = str(fy)
    d["dispCnt"] = "99999"   # 全件
    try:
        res = post(_YAMAGATA_PPI + "JS05-01-01.shtml", d, ref=_YAMAGATA_PPI + "JS05-01-01.shtml")
    except Exception as e:  # noqa: BLE001
        logger.error(f"山形県電子入札検索失敗: {e}")
        return results

    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", res, re.S | re.I):
        am = re.search(r'JS05-02\.shtml\?koujino=(\w+)', tr)
        if not am:
            continue
        koujino = am.group(1)
        anchor = re.search(r'<a\b[^>]*JS05-02[^>]*>(.*?)</a>', tr, re.S)
        cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        title = ""
        if anchor:
            title = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", anchor.group(1)))).strip()
        if not title and len(cells) > 2:
            title = cells[2]
        if not title:
            continue
        published = _yamagata_ppi_date(cells[0]) if len(cells) > 0 else ""
        deadline = _yamagata_ppi_date(cells[1]) if len(cells) > 1 else ""
        org = cells[4].strip() if len(cells) > 4 else "山形県"
        gyoshu = cells[5].strip() if len(cells) > 5 else ""
        method = cells[6].strip() if len(cells) > 6 else ""
        cat = "プロポーザル" if re.search(r"プロポ|企画提案|企画競争|公募型", title + method + gyoshu) else "入札"
        slug = hashlib.md5(("YAMAGATA_EBID" + koujino).encode("utf-8")).hexdigest()[:12]
        results.append({
            "title":           title,
            "category":        cat,
            "organization":    f"山形県（{org}）" if org and org != "山形県" else "山形県",
            "prefecture":      "山形県",
            "published_at":    published,
            "deadline":        deadline,
            "result_date":     "",
            "result_url":      "",
            "project_code":    f"YAMAGATA_EBID-{koujino}",
            "awardee":         "",
            "url":             f"{_YAMAGATA_PPI}JS05-02.shtml?koujino={koujino}",
            "source":          "YAMAGATA_EBID",
            "amount":          "",
            "source_category": gyoshu,
            "summary":         "",
            "detail":          "",
            "tags":            ",".join(generate_tags(title, org)),
        })
    logger.info(f"山形県 電子入札(PPI): {len(results)}件取得")
    return results


async def scrape_yamagata_ebid() -> List[Dict]:
    """山形県 入札情報サービス（工事・コンサル等）の現在公告中の入札を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_yamagata_ebid_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"山形県電子入札スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 大分県 共同利用型 入札情報サービスシステム（DENTYO GPPI・iframe/Struts・cp932）
#   ※神奈川GPPIはSPA(raw不可)だが、大分は旧世代のiframe+サーバレンダHTMLでraw可。
#   既存の静的OITAは委託・プロポ中心 → 電子入札の建設工事(P5510入札公告)を追加。
#   フロー: GET GPPI_MENU → GET GP5000_10F?hdn_dantai=1111(大分県) → 左iframe
#   P5000_MENU → P5510_10(工事の入札公告検索フォーム)を GET → frm_main を harvest
#   し hdn_action=btn_reference・ddl_keisaiNen=年度・ddl_pageSize=大きな値で POST
#   （pageSize大で全件1頁・maxPageNo=1）。結果表の1件=非空セル列 [No,発注部局,入札方式,
#   業種,公告日時,開札予定日時,業務名,場所,連絡]。日付は「R08.07.31 09:02」形式。
#   開札予定日>=今日（現在公告中）のみ採用。
# ---------------------------------------------------------------------------
_OITA_GPPI = "https://www.t-elis.pref.oita.lg.jp/DENTYO/"
_OITA_DANTAI = "1111"  # 大分県


def _oita_gppi_date(s: str) -> str:
    m = re.search(r"R\s*(\d+)\s*\.\s*(\d+)\s*\.\s*(\d+)", s or "")
    if m:
        return f"{2018 + int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return _shimane_wareki(s)


def _scrape_oita_ebid_sync() -> List[Dict]:
    import hashlib
    import urllib.request
    import urllib.parse
    import ssl as _ssl
    import http.cookiejar
    import html as _html
    from datetime import date

    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    except Exception:  # noqa: BLE001
        pass
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(u, ref=None):
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        if ref:
            req.add_header("Referer", ref)
        return op.open(req, timeout=45).read().decode("cp932", "replace")

    def post(u, pairs, ref=None):
        data = urllib.parse.urlencode(pairs, encoding="cp932").encode("ascii")
        req = urllib.request.Request(u, data=data, headers={"User-Agent": "Mozilla/5.0"})
        if ref:
            req.add_header("Referer", ref)
        return op.open(req, timeout=60).read().decode("cp932", "replace")

    today = date.today()
    fy = today.year if today.month >= 4 else today.year - 1
    dantai = _OITA_DANTAI
    results: List[Dict] = []
    try:
        get(_OITA_GPPI + "GPPI_MENU")
        get(_OITA_GPPI + f"GP5000_10F?hdn_dantai={dantai}", ref=_OITA_GPPI + "GPPI_MENU")
        get(_OITA_GPPI + f"P5000_MENU?hdn_dantai={dantai}")
        form = get(_OITA_GPPI + f"P5510_10?hdn_dantai={dantai}", ref=_OITA_GPPI + f"P5000_MENU?hdn_dantai={dantai}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"大分県電子入札セッション確立失敗: {e}")
        return results

    fm = re.search(r'<form\b[^>]*>(.*?)</form>', form, re.S | re.I)
    if not fm:
        logger.error("大分県電子入札: 検索フォーム未検出")
        return results
    block = fm.group(1)
    d = {}
    for inp in re.finditer(r'<input\b[^>]*>', block, re.I):
        nm = re.search(r'name="([^"]*)"', inp.group(0))
        vl = re.search(r'value="([^"]*)"', inp.group(0))
        if nm:
            d[nm.group(1)] = _html.unescape(vl.group(1)) if vl else ""
    for sm in re.finditer(r'<select[^>]*name="([^"]*)"', block):
        d.setdefault(sm.group(1), "")
    d["hdn_action"] = "btn_reference"
    d["hdn_dantai"] = dantai
    d["ddl_keisaiNen"] = str(fy)
    d["ddl_pageSize"] = "2000"   # 全件を1頁に（maxPageNo=1）

    try:
        res = post(_OITA_GPPI + "P5510_10", d, ref=_OITA_GPPI + f"P5510_10?hdn_dantai={dantai}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"大分県電子入札検索失敗: {e}")
        return results

    seen = set()
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", res, re.S | re.I):
        cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        cells = [c for c in cells if c and c != "null"]
        # 公告日時・開札予定日時 が連続する R日付ペアの行のみ採用（発注見通しP5530型の単日付は除外）
        di = None
        for i in range(len(cells) - 1):
            if re.search(r"R\s*\d+\.\d+\.\d+", cells[i]) and re.search(r"R\s*\d+\.\d+\.\d+", cells[i + 1]):
                di = i
                break
        if di is None or di < 3 or di + 2 >= len(cells):
            continue
        published = _oita_gppi_date(cells[di])
        deadline = _oita_gppi_date(cells[di + 1])
        title = cells[di + 2].strip()
        gyoshu = cells[di - 1].strip()
        method = cells[di - 2].strip()
        org = cells[di - 3].strip() or "大分県"
        if not title:
            continue
        # 現在公告中のみ（開札予定日が未到来）。日付不明はキープ。
        if deadline and deadline < today.isoformat():
            continue
        key = re.sub(r"\s+", "", title) + published
        if key in seen:
            continue
        seen.add(key)
        cat = "プロポーザル" if re.search(r"プロポ|企画提案|企画競争|公募型", title + method) else "入札"
        slug = hashlib.md5(("OITA_EBID" + key).encode("utf-8")).hexdigest()[:12]
        results.append({
            "title":           title,
            "category":        cat,
            "organization":    f"大分県（{org}）" if org and org != "大分県" else "大分県",
            "prefecture":      "大分県",
            "published_at":    published,
            "deadline":        deadline,
            "result_date":     "",
            "result_url":      "",
            "project_code":    f"OITA_EBID-{slug}",
            "awardee":         "",
            "url":             f"{_OITA_GPPI}GP5000_10F?hdn_dantai={dantai}#{slug}",
            "source":          "OITA_EBID",
            "amount":          "",
            "source_category": gyoshu,
            "summary":         "",
            "detail":          "",
            "tags":            ",".join(generate_tags(title, org)),
        })
    logger.info(f"大分県 電子入札(GPPI工事): {len(results)}件取得")
    return results


async def scrape_oita_ebid() -> List[Dict]:
    """大分県 共同利用型 入札情報サービス（建設工事の入札公告）の現在公告中を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_oita_ebid_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"大分県電子入札スクレイパー例外: {e}")
        return []


def fetch_shizuoka_detail(url: str) -> Optional[Dict]:
    """静岡県 入札・公募 個別記事ページの本文を取得する（更新日を公示日として抽出）。"""
    import urllib.request
    from urllib.parse import urljoin
    # 電子入札システム(SuperCALS)の案件はセッション依存で詳細確定済み。スキップ。
    if "ebidPPIPublish" in url:
        return None
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"静岡県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find(id="content") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    published_at = ""
    m = re.search(r"更新日\s*\n?\s*(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        published_at = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|zip|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"\s*[（(][^）)]*(?:KB|MB)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            full = urljoin(url, a["href"])
            attachments.append({"name": name, "url": full, "kind": "公募要領"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments,
            "published_at": published_at}


# ---------------------------------------------------------------------------
# 福井県（公募型プロポーザル一覧。全庁横断・単一ページ・ページネーション無し）
# 一般競争入札の全庁横断一覧が無いため、プロポーザルのみ対応。
# robots.txtで "koji_*.pdf" 形式のPDFのみDisallowされているため、添付抽出時に除外する。
# ---------------------------------------------------------------------------
_FUKUI_BASE = "https://www.pref.fukui.lg.jp"
_FUKUI_LIST = _FUKUI_BASE + "/gyosei/tetuduki/cat4502/index.html"
_FUKUI_DISALLOWED_PDF = re.compile(r"koji_.*\.pdf$", re.I)


def _fukui_date_iso(text: str) -> str:
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _scrape_fukui_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    results = []
    try:
        html = get(_FUKUI_LIST)
    except Exception as e:  # noqa: BLE001
        logger.error(f"福井県一覧取得失敗: {e}")
        return results
    rows = re.findall(
        r'<li>\s*<h5><a[^>]*href="([^"]+)">([^<]+)</a><span class="date">\(最終更新日\s*([^)]+)\)</span>',
        html)
    seen = set()
    for href, title, d in rows:
        full = urljoin(_FUKUI_LIST, href)
        if full in seen:
            continue
        seen.add(full)
        title = title.strip()
        if not re.search(r"プロポーザル|企画提案", title):
            # cat4502には補助金案内・入札制度の案内ページ等も混在するため、
            # 実際の公募型プロポーザル案件と分かるタイトルのみ対象とする。
            continue
        slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-")
        results.append({
            "title":           title,
            "category":        "プロポーザル",
            "organization":    "福井県",
            "prefecture":      "福井県",
            "published_at":    _fukui_date_iso(d),
            "deadline":        "",
            "result_date":     "",
            "result_url":      "",
            "project_code":    f"FUKUI-{slug}",
            "awardee":         "",
            "url":             full,
            "source":          "FUKUI",
            "amount":          "",
            "source_category": "",
            "summary":         "",
            "detail":          "",
            "tags":            ",".join(generate_tags(title)),
        })
    logger.info(f"福井県: {len(results)}件取得")
    return results


async def scrape_fukui() -> List[Dict]:
    """福井県公式サイト 公募型プロポーザル一覧（全庁横断）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_fukui_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"福井県スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 福井県 建設工事・業務委託等（福井県電子入札システム SuperCALS「入札情報サービス」）。
# 既存の福井スクレイパー(cat4502)は公募型プロポのみで入札を含まないため、電子入札
# システムの入札予定(公告)をセッションPOST連鎖で取得して補完する。千葉と同じ
# EjPPIj系だが、福井は EbidCD(電子/紙)・SearchDateType・NyusatuHousiki1〜5(入札方式
# チェックボックス)が必須で、これらが無いと「文字列認識失敗エラー」になる。
# 一覧は公告日の昇順・1ページ最大100件。ChoutatsuCD=00(工事)/01(業務委託等)。
# ---------------------------------------------------------------------------
_FUKUI_CALS_EJ = "https://www2.ebid.pref.fukui.jp/ebidPPIPublish/EjPPIj"
_FUKUI_CALS_KIKAN = "0001000"  # 福井県本体（departArray[0]）
_FUKUI_CALS_CHOUTATSU = [("00", "工事"), ("01", "業務委託等")]
_FUKUI_CALS_MAX_DETAIL = 220


def _scrape_fukui_cals_sync() -> List[Dict]:
    import hashlib
    import html as _html
    import time as _time
    import urllib.request
    import urllib.parse
    import http.cookiejar
    from datetime import date, timedelta

    # 締切(入札書受付終了予定日)が過ぎた終了案件を落とす窓。
    cutoff = (date.today() - timedelta(days=1)).isoformat()
    today = date.today()
    fy = today.year if today.month >= 4 else today.year - 1

    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", "Mozilla/5.0"), ("Referer", _FUKUI_CALS_EJ)]

    def post(pairs):
        body = urllib.parse.urlencode(pairs).encode()
        return op.open(_FUKUI_CALS_EJ, data=body, timeout=60).read().decode("cp932", "replace")

    def get(url):
        return op.open(url, timeout=60).read().decode("cp932", "replace")

    results: List[Dict] = []
    detail_budget = _FUKUI_CALS_MAX_DETAIL
    try:
        get(_FUKUI_CALS_EJ)
        post([("ejParameterID", "StartPage")])
    except Exception as e:  # noqa: BLE001
        logger.error(f"福井県電子入札セッション確立失敗: {e}")
        return results

    for cd, cd_label in _FUKUI_CALS_CHOUTATSU:
        try:
            post([("ejParameterID", "EjPSJ01"), ("ejProcessName", "start")])
            get(_FUKUI_CALS_EJ + "?ejParameterID=EjPSJ01&ejShousaiDispFlag=false&ejProcessName=getCondPage")
            lst = post([
                ("Nendo", str(fy)), ("KikanNO", _FUKUI_CALS_KIKAN), ("ejMaxDisplayRowCount", "100"),
                ("BukyokuNO", ""), ("KakakariNO", ""), ("ChoutatsuCD", cd), ("BidSuccessfulMethodType", ""),
                ("EbidCD", "1"),
                ("NyusatuHousiki1", "01"), ("NyusatuHousiki2", "02"), ("NyusatuHousiki3", "03"),
                ("NyusatuHousiki4", "04"), ("NyusatuHousiki5", "05"),
                ("KoujiSyubetu", ""), ("SearchDateType", "1"), ("kkselect", "AND"),
                ("mojisel1", ""), ("mojisel2", ""), ("BidStDate", ""), ("BidEnDate", ""),
                ("getStpos", "0"), ("ejParameterID", "EjPSJ01"), ("ejProcessName", "findList"),
                ("ejShousaiDispFlag", "false"),
            ])
        except Exception as e:  # noqa: BLE001
            logger.error(f"福井県電子入札検索失敗（{cd_label}）: {e}")
            continue

        fv = re.search(r'ejFindVersion"\s*value="(\d+)"', lst)
        find_version = fv.group(1) if fv else ""
        if not find_version:
            logger.info(f"福井県電子入札（{cd_label}）: ejFindVersion取得失敗")
            continue

        for tr in re.findall(r"<TR[^>]*>(.*?)</TR>", lst, re.S | re.I):
            if "openYotei" not in tr:
                continue
            idxm = re.search(r"openYotei\('?(\d+)'?\)", tr)
            if not idxm:
                continue
            idx = idxm.group(1)
            tds = re.findall(r"<TD[^>]*>(.*?)</TD>", tr, re.S)
            cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", c))).replace("\xa0", " ").strip() for c in tds]
            # 列: No, 公告日, 開札予定?, 締切?, 案件名, 場所, 発注機関 …（数はcd依存）
            koukoku = _chiba_cals_wareki(cells[1]) if len(cells) > 1 else ""
            # 案件名: リンクセル（openYoteiを含むTD）のテキスト
            title = ""
            for c in cells:
                if len(c) >= 6 and not re.match(r"^[\dR\-\s:APM/]+$", c) and "福井県" not in c:
                    title = c
                    break
            if not title:
                continue

            org, published, deadline, amount, gyoshu = "福井県", koukoku, "", "", cd_label
            if detail_budget > 0:
                try:
                    dv = post([
                        ("ejParameterID", "EjPSJ01"), ("ejProcessName", "getDetailPage"),
                        ("ejCategoryName", "display"), ("ejKeyNo", idx), ("ejFindVersion", find_version),
                        ("ejStartPosition", "0"), ("ejMaxDisplayRowCount", "100"), ("ejShousaiDispFlag", "false"),
                    ])
                    detail_budget -= 1
                    info = _parse_chiba_cals_detail(dv)
                    org = info["org"] or org
                    published = info["published_at"] or koukoku
                    deadline = info["deadline"]
                    amount = info["amount"]
                    gyoshu = info["gyoshu"] or cd_label
                    _time.sleep(0.25)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"福井県電子入札詳細取得失敗（{title}）: {e}")

            # 締切が過ぎた終了案件は除外（締切不明はキープ）
            if deadline and deadline < cutoff:
                continue

            # slugは実行間で安定な値のみ（idxは毎回変わるため除外。重複蓄積防止）
            slug = hashlib.md5((title + koukoku).encode("utf-8")).hexdigest()[:12]
            results.append({
                "title":           title,
                "category":        "入札",
                "organization":    org,
                "prefecture":      "福井県",
                "published_at":    published,
                "deadline":        deadline,
                "result_date":     "",
                "result_url":      "",
                "project_code":    f"FUKUI-CALS-{slug}",
                "awardee":         "",
                "url":             f"{_FUKUI_CALS_EJ}?KikanNO={_FUKUI_CALS_KIKAN}#{slug}",
                "source":          "FUKUI",
                "amount":          amount,
                "source_category": gyoshu,
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title, org)),
            })
    logger.info(f"福井県 建設工事・業務委託(電子入札): {len(results)}件取得")
    return results


async def scrape_fukui_cals() -> List[Dict]:
    """福井県 建設工事・業務委託等（福井県電子入札システム）の入札予定(公告)を取得する。

    セッション依存のためこの関数内でorganization・公告日・締切まで確定させる
    （fetch_fukui_detailはebidPPIPublish URLをスキップする）。一覧は公告日昇順で
    1ページ100件のため、各調達区分の先頭100件（＝締切が近い順）を対象とする。
    """
    try:
        return await asyncio.to_thread(_scrape_fukui_cals_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"福井県電子入札スクレイパー例外: {e}")
        return []


def fetch_fukui_detail(url: str) -> Optional[Dict]:
    """福井県 入札・公募 個別記事ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    # 電子入札システム(SuperCALS)の案件はセッション依存で詳細確定済み。スキップ。
    if "ebidPPIPublish" in url:
        return None
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"福井県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find(id="content") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if _FUKUI_DISALLOWED_PDF.search(a["href"]):
            continue  # robots.txtでDisallowされているPDFパターン
        if re.search(r"\.(pdf|zip|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"\s*[（(][^）)]*(?:キロバイト|KB|MB)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            full = urljoin(url, a["href"])
            attachments.append({"name": name, "url": full, "kind": "公募要領"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments}


# ---------------------------------------------------------------------------
# 新潟県（「入札・発注・売却」新着一覧。直近50件・全庁横断・ページネーション無し）
# ---------------------------------------------------------------------------
_NIIGATA_BASE = "https://www.pref.niigata.lg.jp"
_NIIGATA_LIST = _NIIGATA_BASE + "/life/sub/8/index-2.html"


def _niigata_date_iso(text: str) -> str:
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{y:04d}-{mo:02d}-{d:02d}"


_NIIGATA_ORG_SUFFIX = re.compile(r"[）)]\s*([^\s（）()「」]{2,20})$")


def _niigata_org(title: str) -> str:
    """タイトル末尾の「（...）出納局会計検査課」のような発注部署名を抽出する（無ければ空）。"""
    m = _NIIGATA_ORG_SUFFIX.search(title)
    return m.group(1) if m else ""


def _scrape_niigata_sync() -> List[Dict]:
    import urllib.request
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    results = []
    try:
        html = get(_NIIGATA_LIST)
    except Exception as e:  # noqa: BLE001
        logger.error(f"新潟県一覧取得失敗: {e}")
        return results
    rows = re.findall(
        r'<span class="article_date">([^<]+)</span>'
        r'<span class="article_title"><a href="([^"]+)">([^<]+)</a>',
        html)
    seen = set()
    for d, href, title in rows:
        full = href if href.startswith("http") else _NIIGATA_BASE + href
        if full in seen:
            continue
        seen.add(full)
        title = title.strip()
        cat = "プロポーザル" if re.search(r"プロポーザル|企画提案|企画競争|デザイン企画コンペ", title) else "入札"
        slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-")
        org = _niigata_org(title)
        results.append({
            "title":           title,
            "category":        cat,
            "organization":    ("新潟県 " + org).strip(),
            "prefecture":      "新潟県",
            "published_at":    _niigata_date_iso(d),
            "deadline":        "",
            "result_date":     "",
            "result_url":      "",
            "project_code":    f"NIIGATA-{slug}",
            "awardee":         "",
            "url":             full,
            "source":          "NIIGATA",
            "amount":          "",
            "source_category": "",
            "summary":         "",
            "detail":          "",
            "tags":            ",".join(generate_tags(title, org)),
        })
    logger.info(f"新潟県: {len(results)}件取得")
    return results


async def scrape_niigata() -> List[Dict]:
    """新潟県公式サイト「入札・発注・売却」新着一覧（直近50件・全庁横断）を取得する。"""
    try:
        return await asyncio.to_thread(_scrape_niigata_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"新潟県スクレイパー例外: {e}")
        return []


def fetch_niigata_detail(url: str) -> Optional[Dict]:
    """新潟県 入札・公募 個別記事ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
    try:
        op = urllib.request.build_opener()
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        html = op.open(url, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"新潟県詳細取得失敗 {url}: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find(id="main_body") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    attachments = []
    for a in main.find_all("a", href=True):
        if re.search(r"\.(pdf|zip|docx?|xlsx?)($|\?)", a["href"], re.I):
            name = re.sub(r"\s*[（(][^）)]*(?:KB|MB)[）)]\s*$", "", a.get_text(" ", strip=True)).strip() or "添付資料"
            full = urljoin(url, a["href"])
            attachments.append({"name": name, "url": full, "kind": "公募要領"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments}


def fetch_niigata_results(max_items: int = 30) -> List[Dict]:
    """新潟県「入札・発注・売却」新着フィードから結果記事（審査結果・入札結果・評価結果）を
    一括取得する。案件名の突合は「件名」欄（入札結果系）があればそれを、無ければ
    （プロポーザル系）ラベルを除いたタイトル自体をbigram類似度の対象として返す。
    """
    import urllib.request
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    try:
        html = get(_NIIGATA_LIST)
    except Exception as e:  # noqa: BLE001
        logger.error(f"新潟県結果一覧取得失敗: {e}")
        return []

    rows = re.findall(
        r'<span class="article_title"><a href="([^"]+)">([^<]+)</a>', html)
    results = []
    seen = set()
    for href, title in rows:
        title = title.strip()
        if not re.search(r"審査結果|入札結果|評価結果", title):
            continue
        full = href if href.startswith("http") else _NIIGATA_BASE + href
        if full in seen:
            continue
        seen.add(full)
        try:
            dhtml = get(full)
        except Exception as e:  # noqa: BLE001
            logger.error(f"新潟県結果詳細取得失敗 {full}: {e}")
            continue
        soup = BeautifulSoup(dhtml, "html.parser")
        main = soup.find(id="main_body") or soup
        text = main.get_text("\n", strip=True)
        m_name = re.search(r"件名[：:]\s*([^\n]+)", text)
        match_text = m_name.group(1).strip() if m_name else re.sub(r"【[^】]*】", "", title).strip()
        m_awardee = re.search(r"契約相手方[：:]\s*([^\n]+)", text)
        awardee = m_awardee.group(1).strip() if m_awardee else ""
        if not awardee:
            m_awardee2 = re.search(r"契約候補者\s+([^\n]+)", text)
            awardee = m_awardee2.group(1).strip() if m_awardee2 else ""
        if not awardee:
            continue
        amount = ""
        m_amount = re.search(r"落札価格[：:]\s*([^\n]+)", text)
        if m_amount:
            amount = m_amount.group(1).strip()
        results.append({
            "title": match_text,
            "bigrams": _title_bigrams(match_text),
            "awardee": awardee,
            "amount": amount,
            "result_date": _niigata_date_iso(text),
        })
        import time as _time
        _time.sleep(0.4)
        if len(results) >= max_items:
            break
    logger.info(f"新潟県 結果記事: {len(results)}件取得")
    return results


# ---------------------------------------------------------------------------
# 石川県（電子入札共同システム「SuperCALS」。セッションCookie＋POSTフォーム連鎖で
# 検索を実行する古いJSPシステム。詳細は下記フローで攻略：
#   1. POST ejParameterID=StartPage&KikanNO=1700100 → JSESSIONID発行
#   2. POST ejParameterID=EjQSJ01&ejProcessName=start → 検索画面へ遷移
#   3. POST ejParameterID=EjQSJ01&ejProcessName=findList（+検索条件） → 一覧取得
#   4. POST ejParameterID=EjQSJ01&ejProcessName=getDetailPage
#      &ejCategoryName=display&ejKeyNo={一覧内index}&ejFindVersion={一覧応答内の値}
#      → 案件詳細取得
# ejFindVersionは検索結果ごとに変わるため、一覧取得と詳細取得は同一セッション内で
# 連続して行う必要がある（他ソースのような「後段で詳細だけ再取得」は不可）。
# ---------------------------------------------------------------------------
_ISHIKAWA_BASE = "https://www.ep-bis.supercals.jp"
_ISHIKAWA_EJ = _ISHIKAWA_BASE + "/ebidPPIGPublish/EjPPIj"
_ISHIKAWA_KIKAN = "1700100"
_ISHIKAWA_PORTAL_URL = f"{_ISHIKAWA_EJ}?KikanNO={_ISHIKAWA_KIKAN}"

_ISHIKAWA_ROW_RE = re.compile(
    r'<TD class="DISP_LIST_L_R">\s*(\d+)\s*</TD>\s*'
    r'<TD class="DISP_LIST_L_C">\s*([^<]*?)\s*</TD>\s*'
    r'<TD class="DISP_LIST_L_L">\s*([^<]*?)\s*</TD>\s*'
    r'<TD class="DISP_LIST_L_C">\s*([^<]*?)\s*</TD>\s*'
    r'<TD class="DISP_LIST_L_L">\s*([^<]*?)\s*</TD>\s*'
    r'<TD class="DISP_LIST_L_C">\s*([^<]*?)\s*</TD>\s*'
    r'<TD class="DISP_LIST_L_R">\s*([^<]*?)\s*</TD>\s*'
    r'<TD class="DISP_LIST_L_C">([^<]*)</TD>\s*'
    r'<TD class="DISP_LIST_L_L"><A href="#" onClick="javascript:openYotei\(\'(\d+)\'\)',
    re.S)


def _ishikawa_wareki_iso(text: str) -> str:
    m = re.search(r"令和\s*0*(\d+)\s*年\s*0*(\d+)\s*月\s*0*(\d+)\s*日", text)
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{2018 + y:04d}-{mo:02d}-{d:02d}"


def _parse_ishikawa_detail(html: str) -> Dict[str, str]:
    """石川県 案件詳細（getDetailPage応答）から組織・公告日・締切を抽出する。"""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    org = ""
    m = re.search(r"令和\d+年度\s*\n(.*?)\n調達案件名称", text, re.S)
    if m:
        org = re.sub(r"\s+", "", m.group(1))
    published_at = ""
    m2 = re.search(r"公告日\n([^\n]+)", text)
    if m2:
        published_at = _ishikawa_wareki_iso(m2.group(1))
    deadline = ""
    # 一般競争・指名競争は「入札書受付日時」、随意契約は「見積書受付締切日時」とラベルが異なる
    m3 = re.search(r"(?:入札書受付日時|見積書受付締切日時)\n(.*?)\n開札予定日時", text, re.S)
    if m3:
        dates = re.findall(r"令和\d+年\d+月\d+日", m3.group(1))
        if dates:
            deadline = _ishikawa_wareki_iso(dates[-1])
    amount = ""
    # 「予定価格」の次行は「（税別）」等の注記のことがあるため、その場合は次の行を値とみなす
    m4 = re.search(r"予定価格\n+(?:[^\n]*[）)]\n+)?([^\n]+)", text)
    if m4 and "非公開" not in m4.group(1):
        amount = m4.group(1).strip()
    return {"org": org, "published_at": published_at, "deadline": deadline,
            "amount": amount, "detail": text[:4000]}


def _scrape_ishikawa_sync() -> List[Dict]:
    import hashlib
    import time as _time
    import urllib.request
    import urllib.parse
    import http.cookiejar

    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def post(data):
        body = urllib.parse.urlencode(data).encode()
        return op.open(_ISHIKAWA_EJ, data=body, timeout=40).read().decode("shift_jis", "replace")

    try:
        post({"ejParameterID": "StartPage", "KikanNO": _ISHIKAWA_KIKAN})
        post({"ejParameterID": "EjQSJ01", "ejProcessName": "start"})
        list_html = post({
            "Nendo": "", "KikanNOnyu": _ISHIKAWA_KIKAN, "BukyokuNOnyu": "", "KakakariNOnyu": "",
            "BidStDate": "", "BidEnDate": "", "ShikakuType": "", "EigyoHinmokuCD": "",
            "SearchString1": "", "kkselect": "AND", "SearchString2": "",
            "ejMaxDisplayRowCount": "50", "ejDisplaySort": "030006", "ejSortSequence": "desc",
            "ejParameterID": "EjQSJ01", "ejProcessName": "findList", "ejShousaiDispFlag": "false",
        })
    except Exception as e:  # noqa: BLE001
        logger.error(f"石川県セッション確立・検索失敗: {e}")
        return []

    rows = _ISHIKAWA_ROW_RE.findall(list_html)
    m = re.search(r'ejFindVersion" value="(\d+)"', list_html)
    find_version = m.group(1) if m else ""
    if not rows or not find_version:
        logger.info("石川県: 該当案件なし、またはejFindVersion取得失敗")
        return []

    results = []
    for _no, bid_date, title, _grade, gyoshu, _method, _price, update_date, idx in rows:
        title = title.strip()
        try:
            dhtml = post({
                "ejParameterID": "EjQSJ01", "ejProcessName": "getDetailPage",
                "ejCategoryName": "display", "ejKeyNo": idx,
                "ejFindVersion": find_version, "ejStartPosition": "0",
                "ejMaxDisplayRowCount": "50", "ejShousaiDispFlag": "false",
            })
        except Exception as e:  # noqa: BLE001
            logger.error(f"石川県詳細取得失敗（{title}）: {e}")
            continue
        info = _parse_ishikawa_detail(dhtml)
        slug = hashlib.md5((title + bid_date + update_date).encode("utf-8")).hexdigest()[:12]
        results.append({
            "title":           title,
            "category":        "入札",
            "organization":    ("石川県 " + info["org"]).strip(),
            "prefecture":      "石川県",
            "published_at":    info["published_at"],
            "deadline":        info["deadline"],
            "result_date":     "",
            "result_url":      "",
            "project_code":    f"ISHIKAWA-{slug}",
            "awardee":         "",
            "url":             f"{_ISHIKAWA_PORTAL_URL}#{slug}",
            "source":          "ISHIKAWA",
            "amount":          info["amount"],
            "source_category": gyoshu.strip(),
            "summary":         "",
            "detail":          info["detail"],
            "tags":            ",".join(generate_tags(title, info["org"])),
        })
        _time.sleep(0.5)
    logger.info(f"石川県: {len(results)}件取得")
    return results


async def scrape_ishikawa() -> List[Dict]:
    """石川県電子入札共同システム（SuperCALS）「入札予定」一覧を取得する。

    一覧・詳細ともにセッション依存のため、他ソースと異なりこの関数だけで
    detail・organization・deadlineまで全て確定させる（後段の詳細取得フェーズは通さない）。
    """
    try:
        return await asyncio.to_thread(_scrape_ishikawa_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"石川県スクレイパー例外: {e}")
        return []


# ---------------------------------------------------------------------------
# 石川県 建設工事・測量コンサル（ep-bis SuperCALS。既存の石川スクレイパーは
# ebidPPIGPublish=物品/役務のみで建設工事が欠落していたため補完する）。
# 建設は ebidPPIPublish（Gなし）/ EjPSJ01（千葉と同型）。ep-bisは複数県共用ホスト
# のため機関番号 KikanNO=1700000（石川県）で絞る。件数が多い（工事だけで千件超）ので
# 公告日ウィンドウ＋詳細取得上限で負荷を抑える。詳細ラベルは締切が無く開札予定日時。
# ---------------------------------------------------------------------------
_ISHIKAWA_CALS_EJ = _ISHIKAWA_BASE + "/ebidPPIPublish/EjPPIj"   # Gなし＝建設系
_ISHIKAWA_CALS_KIKAN = "1700000"  # 石川県（departArray, 物品の1700100とは別）
_ISHIKAWA_CALS_CHOUTATSU = [("00", "工事"), ("01", "測量・コンサル")]
_ISHIKAWA_CALS_WINDOW_DAYS = 30
_ISHIKAWA_CALS_MAX_DETAIL = 300   # 共用ホスト(ep-bis)配慮の詳細取得上限（新着が多い日も工種を拾えるよう引上げ。throttle付き）


def _scrape_ishikawa_cals_sync() -> List[Dict]:
    import hashlib
    import html as _html
    import time as _time
    import urllib.request
    import urllib.parse
    import http.cookiejar
    from datetime import date, timedelta

    today = date.today()
    lo = today - timedelta(days=_ISHIKAWA_CALS_WINDOW_DAYS)
    bs, be = lo.strftime("%Y/%m/%d"), today.strftime("%Y/%m/%d")
    cutoff_iso = lo.isoformat()
    fy = today.year if today.month >= 4 else today.year - 1

    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", "Mozilla/5.0"), ("Referer", _ISHIKAWA_CALS_EJ)]

    def post(pairs):
        body = urllib.parse.urlencode(pairs).encode()
        return op.open(_ISHIKAWA_CALS_EJ, data=body, timeout=60).read().decode("cp932", "replace")

    def get(url):
        return op.open(url, timeout=60).read().decode("cp932", "replace")

    results: List[Dict] = []
    detail_budget = _ISHIKAWA_CALS_MAX_DETAIL
    try:
        get(f"{_ISHIKAWA_CALS_EJ}?KikanNO={_ISHIKAWA_CALS_KIKAN}")
        post([("ejParameterID", "StartPage"), ("KikanNO", _ISHIKAWA_CALS_KIKAN)])
    except Exception as e:  # noqa: BLE001
        logger.error(f"石川県建設セッション確立失敗: {e}")
        return results

    for cd, cd_label in _ISHIKAWA_CALS_CHOUTATSU:
        try:
            post([("ejParameterID", "EjPSJ01"), ("ejProcessName", "start")])
            get(_ISHIKAWA_CALS_EJ + "?ejParameterID=EjPSJ01&ejShousaiDispFlag=false&ejProcessName=getCondPage")
            lst = post([
                ("Nendo", str(fy)), ("KikanNO", _ISHIKAWA_CALS_KIKAN), ("ChoutatsuCD", cd),
                ("BukyokuNO", ""), ("KoujiSyubetu", ""), ("BidStDate", bs), ("BidEnDate", be),
                ("kkselect", "AND"), ("mojisel1", ""), ("mojisel2", ""),
                ("chiiki_dataList", ""), ("chiikisentaku", ""), ("getStpos", "0"), ("AllhitSize", "0"),
                ("ejMaxDisplayRowCount", "700"), ("ejDisplaySort", "030006"), ("ejSortSequence", "desc"),
                ("ejParameterID", "EjPSJ01"), ("ejProcessName", "findList"), ("ejShousaiDispFlag", "false"),
            ])
        except Exception as e:  # noqa: BLE001
            logger.error(f"石川県建設検索失敗（{cd_label}）: {e}")
            continue

        if "多すぎ" in lst or "700件以内" in lst:
            logger.warning(f"石川県建設（{cd_label}）: 件数超過。ウィンドウを狭める必要あり")
        fv = re.search(r'ejFindVersion"\s*value="(\d+)"', lst)
        find_version = fv.group(1) if fv else ""
        if not find_version:
            logger.info(f"石川県建設（{cd_label}）: 該当なしまたはejFindVersion取得失敗")
            continue

        for tr in re.findall(r"<TR[^>]*>(.*?)</TR>", lst, re.S | re.I):
            if "openYotei" not in tr:
                continue
            idxm = re.search(r"openYotei\('?(\d+)'?\)", tr)
            if not idxm:
                continue
            idx = idxm.group(1)
            tds = re.findall(r"<TD[^>]*>(.*?)</TD>", tr, re.S)
            cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", c))).replace("\xa0", " ").strip() for c in tds]
            koukoku = _chiba_cals_wareki(cells[1]) if len(cells) > 1 else ""
            title = re.sub(r"\s*※\s*添付有\s*$", "", cells[2]).strip() if len(cells) > 2 else ""
            price = cells[6] if len(cells) > 6 else ""
            if not title:
                continue
            if koukoku and koukoku < cutoff_iso:
                continue

            org, published, deadline, amount, gyoshu = "石川県", koukoku, "", price, cd_label
            if detail_budget > 0:
                try:
                    dv = post([
                        ("ejParameterID", "EjPSJ01"), ("ejProcessName", "getDetailPage"),
                        ("ejCategoryName", "display"), ("ejKeyNo", idx), ("ejFindVersion", find_version),
                        ("ejStartPosition", "0"), ("ejMaxDisplayRowCount", "700"), ("ejShousaiDispFlag", "false"),
                    ])
                    detail_budget -= 1
                    info = _parse_chiba_cals_detail(dv)
                    org = info["org"] or org
                    published = info["published_at"] or koukoku
                    deadline = info["deadline"]
                    amount = info["amount"] or price
                    gyoshu = info["gyoshu"] or cd_label
                    _time.sleep(0.3)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"石川県建設詳細取得失敗（{title}）: {e}")

            # slugは実行間で安定な値のみ（idxは毎回変わるため除外。重複蓄積防止）
            slug = hashlib.md5((title + koukoku).encode("utf-8")).hexdigest()[:12]
            results.append({
                "title":           title,
                "category":        "入札",
                "organization":    org,
                "prefecture":      "石川県",
                "published_at":    published,
                "deadline":        deadline,
                "result_date":     "",
                "result_url":      "",
                "project_code":    f"ISHIKAWA-CALS-{slug}",
                "awardee":         "",
                "url":             f"{_ISHIKAWA_CALS_EJ}?KikanNO={_ISHIKAWA_CALS_KIKAN}#{slug}",
                "source":          "ISHIKAWA",
                "amount":          amount,
                "source_category": gyoshu,
                "summary":         "",
                "detail":          "",
                "tags":            ",".join(generate_tags(title, org)),
            })
    logger.info(f"石川県 建設工事・測量(電子入札): {len(results)}件取得")
    return results


async def scrape_ishikawa_cals() -> List[Dict]:
    """石川県 建設工事・測量コンサル（ep-bis SuperCALS）の入札予定(公告)を取得する。

    セッション依存のためこの関数内で公告日・開札予定・工事種別・予定価格まで確定させる。
    ep-bisは複数県共用ホストのため詳細取得は上限つき・throttle付きで丁寧にアクセスする。
    """
    try:
        return await asyncio.to_thread(_scrape_ishikawa_cals_sync)
    except Exception as e:  # noqa: BLE001
        logger.error(f"石川県建設スクレイパー例外: {e}")
        return []


# 一覧行のタイトル＋openYoteiインデックスだけを緩く拾う（結果一覧は「入札予定」と
# 列構成が異なる可能性があるため、列数を固定しない）
_ISHIKAWA_RESULT_ROW_RE = re.compile(
    r'<TD class="DISP_LIST_L_L"><A href="#" onClick="javascript:openYotei\(\'(\d+)\'\)"[^>]*>([^<]*)</A>',
    re.S)


def fetch_ishikawa_results() -> List[Dict]:
    """石川県電子入札共同システム「入札結果」（ejParameterID=EjQRJ01）を一括取得する。

    「入札予定」と同じセッション・POST連鎖の仕組みを使い回すが、列構成が異なる
    可能性があるため一覧からはタイトルとopenYoteiインデックスのみを取得し、
    決定事業者・案件名は詳細ページ（getDetailPage）のラベル付きフィールドから
    抽出する（未検証：システム稼働時間内での動作確認が必要）。
    """
    import time as _time
    import urllib.request
    import urllib.parse
    import http.cookiejar

    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def post(data):
        body = urllib.parse.urlencode(data).encode()
        return op.open(_ISHIKAWA_EJ, data=body, timeout=40).read().decode("shift_jis", "replace")

    try:
        post({"ejParameterID": "StartPage", "KikanNO": _ISHIKAWA_KIKAN})
        post({"ejParameterID": "EjQRJ01", "ejProcessName": "start"})
        list_html = post({
            "Nendo": "", "KikanNOnyu": _ISHIKAWA_KIKAN, "BukyokuNOnyu": "", "KakakariNOnyu": "",
            "BidStDate": "", "BidEnDate": "", "ShikakuType": "", "EigyoHinmokuCD": "",
            "SearchString1": "", "kkselect": "AND", "SearchString2": "",
            "ejMaxDisplayRowCount": "50", "ejDisplaySort": "030006", "ejSortSequence": "desc",
            "ejParameterID": "EjQRJ01", "ejProcessName": "findList", "ejShousaiDispFlag": "false",
        })
    except Exception as e:  # noqa: BLE001
        logger.error(f"石川県入札結果セッション確立・検索失敗: {e}")
        return []

    rows = _ISHIKAWA_RESULT_ROW_RE.findall(list_html)
    m = re.search(r'ejFindVersion" value="(\d+)"', list_html)
    find_version = m.group(1) if m else ""
    if not rows or not find_version:
        logger.info("石川県入札結果: 該当案件なし、またはejFindVersion取得失敗")
        return []

    results = []
    for idx, _title in rows:
        try:
            dhtml = post({
                "ejParameterID": "EjQRJ01", "ejProcessName": "getDetailPage",
                "ejCategoryName": "display", "ejKeyNo": idx,
                "ejFindVersion": find_version, "ejStartPosition": "0",
                "ejMaxDisplayRowCount": "50", "ejShousaiDispFlag": "false",
            })
        except Exception as e:  # noqa: BLE001
            logger.error(f"石川県入札結果詳細取得失敗（index={idx}）: {e}")
            continue
        soup = BeautifulSoup(dhtml, "html.parser")
        text = soup.get_text("\n", strip=True)
        m_name = re.search(r"調達案件名称\n([^\n]+)", text)
        m_awardee = re.search(r"(?:落札者|契約の相手方|受注者)\s*\n?\s*([^\n]+)", text)
        if not m_name or not m_awardee:
            continue
        results.append({
            "title": m_name.group(1).strip(),
            "bigrams": _title_bigrams(m_name.group(1).strip()),
            "awardee": m_awardee.group(1).strip(),
            "result_date": _ishikawa_wareki_iso(text),
        })
        _time.sleep(0.5)
    logger.info(f"石川県 入札結果: {len(results)}件取得")
    return results


def _parse_fukuoka_award_page(html: str) -> Dict[str, str]:
    """「落札者の公示」記事本文から決定事業者・金額・決定日を抽出する。"""
    soup = BeautifulSoup(html, "html.parser")
    main = (soup.find("main") or soup.find("article")
            or soup.find(id=re.compile(r"content|main", re.I))
            or soup.find(attrs={"class": re.compile(r"article|content|honbun|main", re.I)}))
    node = main if main else soup
    for tag in node.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    flat = re.sub(r"\s+", "", node.get_text("\n", strip=True))

    awardee = ""
    # 表記が「氏名」「落札業者氏名」等揺れるため、本文中で最後に出現する「氏名」ラベルの
    # 直後（＝実際の値の直前）から、次の「住所/所在地」ラベルか丸括弧番号までを値とみなす。
    # 除外文字クラスにすると社名末尾の「事業所」等の「所」で誤って途切れるため使わない。
    idx = flat.rfind("氏名")
    if idx >= 0:
        tail = flat[idx + 2: idx + 2 + 100]
        m = re.match(r"(.+?)(?:[（(][０-９0-9]{1,2}[）)]|住所|所在地)", tail)
        if m:
            awardee = m.group(1).strip("　 ・:：")
    amount = ""
    am = re.search(r"落札金額\s*([0-9,，][0-9,，]*円)", flat)
    if am:
        amount = am.group(1)
    result_date = _fukuoka_wareki_iso(
        (re.search(r"落札者を決定した日([^\d]{0,3}令和\d+年\d+月\d+日)", flat) or [None, ""])[1] or flat)
    return {"awardee": awardee, "amount": amount, "result_date": result_date}


def fetch_fukuoka_results(max_pages: int = 5) -> Dict[str, Dict]:
    """福岡県の「落札者の公示」記事を一括取得し、{契約名称: {awardee, amount, result_date}} を返す。

    福岡県は入札公告と結果公示が別記事でIDの紐づけが無いため、記事タイトルの括弧内にある
    契約名称で後から突合する（愛知県の一括取得方式と同じ考え方）。直近数ページのみ走査する
    （新しい公示ほど先頭に出るため）。
    """
    import urllib.request, time as _time
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    out = {}
    for page in range(1, max_pages + 1):
        url = _FUKUOKA_LIST if page == 1 else f"{_FUKUOKA_LIST}?page={page}"
        try:
            html = get(url)
        except Exception as e:  # noqa: BLE001
            logger.error(f"福岡県 落札者公示一覧取得失敗（page={page}）: {e}")
            break
        links = re.findall(r'<span class="article_title"><a href="([^"]+)">([^<]+)</a></span>', html)
        if not links:
            break
        for href, title in links:
            title = title.strip()
            if not _FUKUOKA_AWARD_LABEL.search(title):
                continue
            contract_name = _fukuoka_award_contract_name(title)
            if not contract_name or contract_name in out:
                continue
            full = href if href.startswith("http") else _FUKUOKA_BASE + href
            try:
                detail_html = get(full)
            except Exception as e:  # noqa: BLE001
                logger.error(f"福岡県 落札者公示取得失敗 {full}: {e}")
                continue
            rec = _parse_fukuoka_award_page(detail_html)
            if rec.get("awardee"):
                out[contract_name] = rec
            _time.sleep(0.6)
        _time.sleep(0.5)
    logger.info(f"福岡県 落札者の公示: {len(out)}件取得")
    return out


# ---------------------------------------------------------------------------
# 全スクレイパー統合
# ---------------------------------------------------------------------------
async def run_all_scrapers(portal_date_from: str = "", jogmec_max_id: int = 0) -> List[Dict]:
    """全スクレイパーを実行して取得結果（生データ）を返す。

    portal_date_from: 調達ポータルの取得開始日（YYYY/MM/DD）。
    jogmec_max_id: 既存CSVのJOGMEC最大ID。増分取得に使用。
    """
    all_results: List[Dict] = []

    tasks = [
        scrape_nedo(),
        scrape_jst(),
        scrape_portal(date_from=portal_date_from),
        scrape_jogmec(max_id=jogmec_max_id),
        scrape_aichi(),
        scrape_aichi_proposal(),
        scrape_tokyo(),
        scrape_osaka(),
        scrape_osaka_proposal(),
        scrape_fukuoka(),
        scrape_mie(),
        scrape_mie_efftis(),
        scrape_gifu(),
        scrape_yamanashi(),
        scrape_toyama(),
        scrape_nagano(),
        scrape_nagano_cals(),
        scrape_shizuoka(),
        scrape_shizuoka_cals(),
        scrape_fukui(),
        scrape_fukui_cals(),
        scrape_niigata(),
        scrape_ishikawa(),
        scrape_ishikawa_cals(),
        scrape_tochigi(),
        scrape_chiba(),
        scrape_chiba_cals(),
        scrape_kyoto(),
        scrape_hyogo(),
        scrape_hyogo_results(),
        scrape_shiga(),
        scrape_wakayama(),
        scrape_hiroshima(),
        scrape_okayama(),
        scrape_ehime(),
        scrape_kochi(),
        scrape_saga(),
        scrape_miyazaki(),
        scrape_niigata_cals(),
        scrape_toyama_cals(),
        scrape_shimane(),
        scrape_kumamoto(),
        scrape_hokkaido(),
        scrape_tokushima(),
        scrape_nagasaki(),
        scrape_okinawa(),
        scrape_oita(),
        scrape_akita(),
        scrape_fukushima(),
        scrape_tottori(),
        scrape_gunma(),
        scrape_saitama(),
        scrape_iwate(),
        scrape_kagoshima(),
        scrape_kagawa(),
        scrape_aomori(),
        scrape_yamagata(),
        scrape_yamaguchi(),
        scrape_miyagi(),
        scrape_kyoto_ebid(),
        scrape_kagawa_ebid(),
        scrape_shiga_ebid(),
        scrape_shimane_ebid(),
        scrape_yamagata_ebid(),
        scrape_oita_ebid(),
        scrape_tochigi_cals(),
    ]

    scraped = await asyncio.gather(*tasks, return_exceptions=True)
    for result in scraped:
        if isinstance(result, list):
            all_results.extend(result)
        elif isinstance(result, Exception):
            logger.error(f"スクレイパーで例外: {result}")

    logger.info(f"合計 {len(all_results)}件")
    return all_results
