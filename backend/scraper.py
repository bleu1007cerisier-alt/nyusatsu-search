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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NyusatsuSearch/1.0; +https://nyusatsu-search.onrender.com/)"
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


async def fetch_bytes(session: aiohttp.ClientSession, url: str, retries: int = 3):
    """URLを取得して (本文バイト列, Content-Type) を返す。失敗時はリトライ、最終的に (b"", "")。"""
    for attempt in range(retries):
        try:
            await asyncio.sleep(0.7)  # サーバー負荷対策
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                raw = await resp.read()
                return raw, resp.headers.get("Content-Type", "")
        except Exception as e:
            if attempt < retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            logger.error(f"取得失敗 {url}: {e}")
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
            raw, ct = await fetch_bytes(session, NEDO_BASE + ylist)
            if not raw:
                continue
            soup = BeautifulSoup(_decode(raw, ct), "html.parser")
            for a in soup.find_all("a", href=re.compile(r"/koubo/20\d\d_list_[0-9_]+\.html")):
                field_pages.setdefault(a["href"], a.get_text(strip=True))

        logger.info(f"NEDO: 分野ページ {len(field_pages)}件を巡回")

        for href, field_name in field_pages.items():
            raw, ct = await fetch_bytes(session, NEDO_BASE + href)
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
        raw, ct = await fetch_bytes(session, JST_BASE + JST_BOSYU)
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
    raw, ct = await fetch_bytes(session, PORTAL_FORM)
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
        ph = {**HEADERS, "Referer": PORTAL_FORM,
              "Content-Type": "application/x-www-form-urlencoded"}

        # 初回 POST で検索実行
        await asyncio.sleep(0.7)
        async with session.post(PORTAL_SEARCH, data=form_data, headers=ph,
                                allow_redirects=True) as resp:
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
                    headers=HEADERS
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
    # 掲載日（一覧ページには日付が無いため、本文の「掲載日：YYYY年M月D日」から取る）
    published_at = ""
    m = re.search(r"掲載日[：:]\s*(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", text)
    if m:
        published_at = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
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
    attachments = []
    for a in (main or soup).find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.pdf($|\?)", href, re.I):
            name = a.get_text(" ", strip=True) or "添付資料"
            full = href if href.startswith("http") else _PREF_OSAKA + href
            attachments.append({"name": name, "url": full, "kind": "公募要領"})
    return {"detail": text[:6000], "budget": "", "schedule": [], "attachments": attachments}


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
    main = soup.find(id="tmp_contents") or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    published_at = ""
    m = re.search(r"公示日\s*\n?\s*(\d{4})年(\d{2})月(\d{2})日", text)
    if m:
        published_at = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
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
_SHIGA_LIST = "https://www.pref.shiga.lg.jp/zigyousya/nyusatsubaikyaku/itaku/"
_SHIGA_ROW = re.compile(
    r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})[^"]*"[^>]*>.*?'
    r'<a href="([^"]+)">([^<]+)</a>', re.S)


def _scrape_shiga_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    try:
        html_doc = op.open(_SHIGA_LIST, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"滋賀県一覧取得失敗: {e}")
        return []
    results, seen = [], set()
    for m in _SHIGA_ROW.finditer(html_doc):
        date_iso, href, raw_title = m.group(1), m.group(2), m.group(3).strip()
        if ".htm" not in href:
            continue
        url = urljoin(_SHIGA_LIST, href)
        if url in seen:
            continue
        seen.add(url)
        # 「公募型プロポーザルの公告（案件名）」→ 案件名を取り出す
        mt = re.search(r"公告(?:（|\()(.+?)(?:）|\))\s*$", raw_title)
        title = (mt.group(1) if mt else raw_title).strip()
        if len(title) < 4:
            continue
        cat = "プロポーザル" if "プロポーザル" in raw_title or "企画提案" in raw_title else "入札"
        slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-") or str(len(seen))
        results.append({
            "title":           title,
            "category":        cat,
            "organization":    "滋賀県",
            "prefecture":      "滋賀県",
            "published_at":    date_iso,
            "deadline":        "",
            "result_date":     "",
            "result_url":      "",
            "project_code":    f"SHIGA-{slug}",
            "awardee":         "",
            "url":             url,
            "source":          "SHIGA",
            "amount":          "",
            "source_category": "",
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
_HIROSHIMA_CATEGORIES = [
    ("/site/nyusatsukeiyaku/list945-4044.html", "物品"),
    ("/site/nyusatsukeiyaku/list945-4041.html", "委託・役務"),
    ("/site/nyusatsukeiyaku/list945-5244.html", "電子入札公告"),
]
_HIROSHIMA_ROW = re.compile(
    r'<span class=article_title><a href="([^"]+)">([^<]+)</a></span>'
    r'\s*<span class=article_date>([^<]+)</span>', re.S)


def _scrape_hiroshima_sync() -> List[Dict]:
    import urllib.request
    from urllib.parse import urljoin
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    results, seen = [], set()
    for path, cat_label in _HIROSHIMA_CATEGORIES:
        try:
            html_doc = op.open(_HIROSHIMA_BASE + path, timeout=40).read().decode("utf-8", "replace")
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
            seen.add(url)
            title = re.sub(r"^【[^】]*】\s*", "", title).strip()
            if len(title) < 4:
                continue
            dm = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", date_raw)
            pub = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}" if dm else ""
            cat = "プロポーザル" if re.search(r"プロポーザル|企画提案|公募型", title) else "入札"
            slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-") or str(len(seen))
            results.append({
                "title":           title,
                "category":        cat,
                "organization":    "広島県",
                "prefecture":      "広島県",
                "published_at":    pub,
                "deadline":        "",
                "result_date":     "",
                "result_url":      "",
                "project_code":    f"HIROSHIMA-{slug}",
                "awardee":         "",
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
    bs, be = lo.strftime("%Y/%m/%d"), today.strftime("%Y/%m/%d")
    fy = today.year if today.month >= 4 else today.year - 1
    status_open = cfg.get("status_open")
    status_col = cfg.get("status_col", 4)
    title_from = cfg.get("title_from", "list")
    title_col = cfg.get("title_col", 2)
    sleep = cfg.get("sleep", 0.25)
    budget = cfg.get("max_detail", 250)

    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
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

            org, published, deadline, amount, gyoshu, dtitle = pref, "", "", "", cd_label, ""
            if budget > 0:
                try:
                    dv = post([
                        ("ejParameterID", "EjPSJ01"), ("ejProcessName", "getDetailPage"),
                        ("ejCategoryName", "display"), ("ejKeyNo", idx), ("ejFindVersion", find_version),
                        ("ejStartPosition", "0"), ("ejMaxDisplayRowCount", "700"), ("ejShousaiDispFlag", "false"),
                    ])
                    budget -= 1
                    info = _parse_chiba_cals_detail(dv)
                    org = info["org"] or pref
                    published = info["published_at"]
                    deadline = info["deadline"]
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
_ISHIKAWA_CALS_MAX_DETAIL = 150   # 共用ホスト(ep-bis)配慮の詳細取得上限


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
    ]

    scraped = await asyncio.gather(*tasks, return_exceptions=True)
    for result in scraped:
        if isinstance(result, list):
            all_results.extend(result)
        elif isinstance(result, Exception):
            logger.error(f"スクレイパーで例外: {result}")

    logger.info(f"合計 {len(all_results)}件")
    return all_results
