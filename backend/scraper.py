"""
スクレイパー：官公庁・公的機関の入札・公募情報を収集する。

設計方針:
  - 文字コードは自動判定（日本語の官公庁サイトは Shift_JIS が多い）
  - 静的HTMLで一覧を公開している、確実に取得できるソースのみを対象とする
  - 一覧 → 各詳細ページを取得し、締切・概要・タグまで収集する
  - 取得に失敗した場合のみサンプルデータにフォールバックする

現在の対象:
  - NEDO（新エネルギー・産業技術総合開発機構）公募情報
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


def _extract_overview(soup: BeautifulSoup) -> str:
    """詳細ページから「公募内容の要約」を抽出する。

    定型の募集アナウンスや手続き案内（説明会・応募方法・契約等）を除き、
    事業の目的・内容を説明する段落を優先して集める。
    """
    def collect(skip_boiler: bool) -> List[str]:
        paras: List[str] = []
        for p in soup.find_all("p"):
            t = p.get_text(strip=True)
            if len(t) < 25 or t.startswith("※"):
                continue
            if skip_boiler and _SKIP_PARA.search(t):
                continue
            paras.append(t)
            if len(paras) >= 4:
                break
        return paras

    paras = collect(skip_boiler=True)
    if not paras:  # 全て定型文だった場合のフォールバック
        paras = collect(skip_boiler=False)
    return "\n\n".join(paras)[:1200]


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
    m = re.search(
        r"予算規模[：:]?[はを]?([0-9０-９,，\.億万千百円以内未満程度税込税抜（）\(\)約\-―~～／/件]{2,45})",
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
# 取得対象の年度別一覧（現行年度のみ。過去年度を足せば件数を増やせる）
NEDO_YEAR_LISTS = ["/koubo/2026_list.html"]

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

                tags = generate_tags(title, field_name, extra=[field_name])
                results.append({
                    "title": title,
                    "category": "プロポーザル",
                    "organization": "NEDO（新エネルギー・産業技術総合開発機構）",
                    "deadline": shimekiri,
                    "published_at": kaishi or yokoku,
                    "result_date": kekka,
                    "result_url": result_url,
                    "project_code": _project_code(title),
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
    """NEDO公募詳細ページを同期取得し、概要と予算規模を返す（データ蓄積時に使用）。"""
    soup = _fetch_soup(url)
    if soup is None:
        return {}
    text = soup.get_text("\n", strip=True)
    return {"detail": _extract_overview(soup), "budget": _extract_budget(text)}


def fetch_nedo_result(url: str) -> Dict[str, str]:
    """NEDO結果（実施体制の決定）ページから決定事業者を返す。"""
    soup = _fetch_soup(url)
    if soup is None:
        return {}
    text = soup.get_text("\n", strip=True)
    return {"awardee": _extract_awardee(text)}


async def run_all_scrapers() -> List[Dict]:
    """全スクレイパーを実行して取得結果（生データ）を返す。"""
    all_results: List[Dict] = []

    tasks = [
        scrape_nedo(),
    ]

    scraped = await asyncio.gather(*tasks, return_exceptions=True)
    for result in scraped:
        if isinstance(result, list):
            all_results.extend(result)
        elif isinstance(result, Exception):
            logger.error(f"スクレイパーで例外: {result}")

    logger.info(f"合計 {len(all_results)}件")
    return all_results
