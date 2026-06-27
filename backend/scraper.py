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
TAG_KEYWORDS = {
    "洋上風力": ["洋上風力", "浮体式", "着床式"],
    "風力": ["風力発電", "風車"],
    "水素": ["水素"],
    "アンモニア": ["アンモニア"],
    "太陽光": ["太陽光", "ペロブスカイト", "太陽電池"],
    "蓄電池": ["蓄電池", "電池", "バッテリー"],
    "脱炭素・GX": ["カーボンニュートラル", "脱炭素", "二酸化炭素", "ＣＯ２", "CO2", "グリーンイノベーション", "ＧＸ", "GX"],
    "資源・リサイクル": ["リサイクル", "資源循環", "廃棄物", "リチウム", "レアメタル", "金属鉱"],
    "AI・データ": ["ＡＩ", "AI", "人工知能", "機械学習", "ビッグデータ", "デジタル", "ＤＸ", "DX"],
    "半導体・レーザー": ["半導体", "レーザー", "ＬｉＤＡＲ", "LiDAR"],
    "ロボット・ドローン": ["ロボット", "ドローン", "自動化"],
    "モビリティ": ["自動車", "モビリティ", "ＥＶ", "車載", "航空", "船舶"],
    "バイオ・医療": ["バイオ", "医療", "ヘルスケア", "創薬", "細胞", "脳", "ニューロ", "ブレインテック"],
    "宇宙": ["宇宙", "衛星"],
    "量子": ["量子"],
    "経済安全保障": ["経済安全保障"],
    "国際": ["国際", "海外", "アジア", "グローバル"],
    "調査": ["調査", "俯瞰"],
    "実証": ["実証"],
    "研究開発": ["研究開発", "技術開発"],
    "補助金・助成": ["補助金", "助成", "懸賞金"],
    "委託": ["委託"],
}


def generate_tags(*texts: str, extra: Optional[List[str]] = None) -> List[str]:
    """タイトル・本文などからタグを自動生成する。"""
    blob = " ".join(t for t in texts if t)
    tags: List[str] = []
    for tag, kws in TAG_KEYWORDS.items():
        if any(kw in blob for kw in kws):
            tags.append(tag)
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
    for m in re.finditer(r"(?:実施予定先|委託予定先|委託先|採択予定先|採択先|代表機関)[：:]?", flat):
        seg = flat[m.end():m.end() + 160]
        # ラベル直後が会社・機関名で始まる箇所のみ採用（説明文や添付参照を除外）
        if not re.match(_ORG_RE, seg):
            continue
        # 次の節（番号付き見出し等）までを決定事業者の記載とみなす
        seg = re.split(r"\d[．.]|事業期間|募集要項|技術・事業分野|お問|（法人番号|採択審査|なお[、，]", seg)[0]
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
                tags = generate_tags(title_clean, field_name, extra=[field_name])
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
                    "summary": field_name,
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

                tags = generate_tags(title, field, extra=[field] if field else None)
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
                    "summary": field,
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
    """
    soup = _fetch_soup(url)
    if soup is None:
        return {}
    text = soup.get_text("\n", strip=True)
    budget = _extract_budget(text)
    if not budget:
        budget = _pdf_budget_from_soup(soup, url)  # JST: ページ URL を渡して相対パスを正解決
    return {
        "detail": _extract_overview(soup),
        "budget": budget,
        "schedule": _extract_schedule(text),
        "attachments": _jst_extract_attachments(soup, url),
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


async def scrape_portal(date_from: str = "") -> List[Dict]:
    """調達ポータルから差分を取得する。

    date_from: 取得開始日（YYYY/MM/DD 形式）。省略時は当日のみ。
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
                    "category":     "入札・公募",  # 詳細取得後に上書き
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

    # 公開終了日 = 締切
    deadline = ""
    for m in re.finditer(r"公開終了日\s*[\n\s]*(.{3,30})", text):
        deadline = _reiwa_date(m.group(1))
        if deadline:
            break

    # 公告内容・分類・調達品目分類 を th → td から直接取得
    # （soup.get_text は改行区切りで URL が次行になるためregexでは取りこぼす）
    _EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
    raw_kouji = ""
    bunrui = ""
    hinmoku = ""
    for th in soup.find_all("th"):
        th_text = th.get_text(strip=True)
        td = th.find_next_sibling("td") or (th.parent.find_next_sibling("tr") and
             th.parent.find_next_sibling("tr").find("td"))
        if not td:
            continue
        val = td.get_text(" ", strip=True)
        if th_text == "公告内容" and not raw_kouji:
            raw_kouji = _EMAIL_RE.sub("", val).strip()  # メールアドレスを除去
        elif th_text == "分類" and not bunrui:
            bunrui = val
        elif th_text == "調達品目分類" and not hinmoku:
            hinmoku = val.strip()

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
# 全スクレイパー統合
# ---------------------------------------------------------------------------
async def run_all_scrapers(portal_date_from: str = "") -> List[Dict]:
    """全スクレイパーを実行して取得結果（生データ）を返す。

    portal_date_from: 調達ポータルの取得開始日（YYYY/MM/DD）。
    build_dataset.py が既存CSVの最終PORTAL掲載日から算出して渡す。
    """
    all_results: List[Dict] = []

    tasks = [
        scrape_nedo(),
        scrape_jst(),
        scrape_portal(date_from=portal_date_from),
    ]

    scraped = await asyncio.gather(*tasks, return_exceptions=True)
    for result in scraped:
        if isinstance(result, list):
            all_results.extend(result)
        elif isinstance(result, Exception):
            logger.error(f"スクレイパーで例外: {result}")

    logger.info(f"合計 {len(all_results)}件")
    return all_results
