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
            k = _normalize_for_tags(kw.lstrip("!"))
            if re.fullmatch(r"[a-z0-9&\-\.]+", k) and len(k) <= 8:
                pats.append((scope, re.compile(
                    r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])")))
            else:
                pats.append((scope, k))  # 文字列＝単純部分一致
        matchers.append((tag, pats))
    return matchers


_TAG_MATCHERS = _compile_tag_matchers()


def generate_tags(*texts: str, extra: Optional[List[str]] = None) -> List[str]:
    """タイトル・要約・本文などから実務粒度のタグを自動付与する。

    texts[0]（タイトル）、texts[:2]（タイトル＋要約等）、全文の3階層で照合する。
    "!!" キーワード＝タイトルのみ / "!" ＝タイトル＋要約 / 無印＝全文。
    """
    title_only = _normalize_for_tags(texts[0] if texts else "")
    primary = _normalize_for_tags(" ".join(t for t in texts[:2] if t))
    blob = _normalize_for_tags(" ".join(t for t in texts if t))
    targets = (blob, primary, title_only)  # scope=0,1,2
    tags: List[str] = []
    for tag, pats in _TAG_MATCHERS:
        for scope, p in pats:
            target = targets[scope]
            hit = p.search(target) if hasattr(p, "search") else (p in target)
            if hit:
                tags.append(tag)
                break
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
    ]

    scraped = await asyncio.gather(*tasks, return_exceptions=True)
    for result in scraped:
        if isinstance(result, list):
            all_results.extend(result)
        elif isinstance(result, Exception):
            logger.error(f"スクレイパーで例外: {result}")

    logger.info(f"合計 {len(all_results)}件")
    return all_results
