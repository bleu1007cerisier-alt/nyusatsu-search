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
        if th_text == "公告内容" and not raw_kouji:
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
        # 記事ページ（.html）かつ 募集・委託・公募・プロポーザル系のみ
        if not re.search(r"\.html?($|\?)", href):
            continue
        if not re.search(r"募集|委託|公募|プロポーザル|選定|企画提案", title):
            continue
        # 先頭の【…】更新マーカーを除去
        title = re.sub(r"^【[^】]*】\s*", "", title).strip()
        if len(title) < 6:
            continue
        url = href if href.startswith("http") else _PREF_AICHI + href
        if url in seen:
            continue
        seen.add(url)
        slug = re.sub(r"[^A-Za-z0-9_.\-]", "_", url.split("//", 1)[-1])[-60:]
        results.append({
            "title":           title,
            "category":        "プロポーザル",
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
                "searchShowRange": "50", "showRange": "50",
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


def _scrape_osaka_proposal_sync() -> List[Dict]:
    import urllib.request
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]
    try:
        html = op.open(_OSAKA_PROP_LIST, timeout=40).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        logger.error(f"大阪府プロポーザル一覧取得失敗: {e}")
        return []
    soup = BeautifulSoup(html, "html.parser")
    main = (soup.find("main") or soup.find(id=re.compile(r"content|main", re.I))
            or soup.find(attrs={"class": re.compile(r"article|content|honbun|main", re.I)})
            or soup)
    results = []
    seen = set()
    for a in main.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True)
        if not re.search(r"\.html?($|\?)", href, re.I):
            continue
        if not re.search(r"募集|提案|プロポーザル|公募|選定|委託先", text):
            continue
        full = href if href.startswith("http") else _PREF_OSAKA + href
        if full in seen or "puropo.html" in full:
            continue
        seen.add(full)
        title = re.sub(r"^【[^】]*】\s*", "", text).strip()
        slug = re.sub(r"[^A-Za-z0-9]+", "-", href.rsplit("/", 1)[-1]).strip("-") or str(len(seen))
        results.append({
            "title":           title,
            "category":        "プロポーザル",
            "organization":    "大阪府",
            "prefecture":      "大阪府",
            "published_at":    "",
            "deadline":        "",
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
            "tags":            ",".join(generate_tags(title)),
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


def _scrape_mie_sync(max_pages: int = 2) -> List[Dict]:
    import urllib.request
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", "Mozilla/5.0")]

    def get(url):
        return op.open(url, timeout=40).read().decode("utf-8", "replace")

    results = []
    seen = set()
    for path, cat in _MIE_CATEGORIES:
        for page in range(1, max_pages + 1):
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


def fetch_mie_detail(url: str) -> Optional[Dict]:
    """三重県 入札・公募 個別記事ページの本文を取得する。"""
    import urllib.request
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


def _scrape_gifu_sync(max_pages: int = 2) -> List[Dict]:
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
        for page in range(1, max_pages + 1):
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
                full = href if href.startswith("http") else _GIFU_BASE + href
                if full in seen:
                    continue
                seen.add(full)
                new_count += 1
                title = title.strip()
                pub = _gifu_date_iso(d)
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


def fetch_nagano_detail(url: str) -> Optional[Dict]:
    """長野県 入札・公募 個別記事ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
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


def fetch_shizuoka_detail(url: str) -> Optional[Dict]:
    """静岡県 入札・公募 個別記事ページの本文を取得する（更新日を公示日として抽出）。"""
    import urllib.request
    from urllib.parse import urljoin
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


def fetch_fukui_detail(url: str) -> Optional[Dict]:
    """福井県 入札・公募 個別記事ページの本文を取得する。"""
    import urllib.request
    from urllib.parse import urljoin
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
        scrape_gifu(),
        scrape_yamanashi(),
        scrape_toyama(),
        scrape_nagano(),
        scrape_shizuoka(),
        scrape_fukui(),
        scrape_niigata(),
        scrape_ishikawa(),
    ]

    scraped = await asyncio.gather(*tasks, return_exceptions=True)
    for result in scraped:
        if isinstance(result, list):
            all_results.extend(result)
        elif isinstance(result, Exception):
            logger.error(f"スクレイパーで例外: {result}")

    logger.info(f"合計 {len(all_results)}件")
    return all_results
