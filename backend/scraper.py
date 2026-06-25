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


async def fetch_bytes(session: aiohttp.ClientSession, url: str):
    """URLを取得して (本文バイト列, Content-Type) を返す。失敗時は (b"", "")。"""
    try:
        await asyncio.sleep(0.7)  # サーバー負荷対策
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            raw = await resp.read()
            return raw, resp.headers.get("Content-Type", "")
    except Exception as e:
        logger.error(f"取得失敗 {url}: {e}")
        return b"", ""


def _normalize_date(text: str) -> str:
    """文字列中の最後の日付を 'YYYY-MM-DD' に正規化する（期間表記は終了日＝締切を採用）。"""
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


def _extract_overview(soup: BeautifulSoup) -> str:
    """詳細ページから概要本文（最初のまとまった段落）を抽出する。"""
    paras: List[str] = []
    for p in soup.find_all("p"):
        t = p.get_text(strip=True)
        if len(t) >= 20 and not t.startswith("※"):
            paras.append(t)
        if len(paras) >= 6:
            break
    return "\n\n".join(paras)[:1500]


NEDO_BASE = "https://www.nedo.go.jp"
# 取得対象の年度別一覧（現行年度のみ。過去年度を足せば件数を増やせる）
NEDO_YEAR_LISTS = ["/koubo/2026_list.html"]


def _clean_nedo_title(text: str) -> str:
    """分野ページの行テキストから日付以降を除いてタイトルを得る。"""
    return re.sub(r"\s*20\d\d\D.*$", "", text).strip()


async def scrape_nedo() -> List[Dict]:
    """NEDO 公募情報を分野別ページから網羅的に取得する。

    年度別一覧 → 分野ページ → 各分野ページ内の個別公募（タイトル・公示日・締切日）を収集する。
    概要本文（detail）は件数が多いため、ここでは取得せず詳細表示時に遅延取得する。
    """
    results: List[Dict] = []
    seen: set = set()

    async with aiohttp.ClientSession() as session:
        # 1) 年度別一覧から分野ページURLを集める
        field_pages: Dict[str, str] = {}
        for ylist in NEDO_YEAR_LISTS:
            raw, ct = await fetch_bytes(session, NEDO_BASE + ylist)
            if not raw:
                continue
            soup = BeautifulSoup(_decode(raw, ct), "html.parser")
            for a in soup.find_all("a", href=re.compile(r"/koubo/20\d\d_list_[0-9_]+\.html")):
                field_pages.setdefault(a["href"], a.get_text(strip=True))

        logger.info(f"NEDO: 分野ページ {len(field_pages)}件を巡回")

        # 2) 各分野ページから個別公募を抽出
        for href, field_name in field_pages.items():
            raw, ct = await fetch_bytes(session, NEDO_BASE + href)
            if not raw:
                continue
            soup = BeautifulSoup(_decode(raw, ct), "html.parser")
            for a in soup.find_all("a", href=re.compile(r"/koubo/[A-Za-z0-9_]+\.html")):
                h = a["href"]
                if "_list" in h or "/index" in h or "pastbunya" in h:
                    continue
                li = a.find_parent(["li", "tr"])
                row_text = li.get_text(" ", strip=True) if li else a.get_text(strip=True)
                if len(row_text) < 6:
                    continue

                full_url = NEDO_BASE + h
                if full_url in seen:
                    continue
                seen.add(full_url)

                title = _clean_nedo_title(row_text) or a.get_text(strip=True)
                if not title or len(title) < 6:
                    continue

                # 行内の日付：最初＝公示日、最後＝締切日（複数回ある場合は最終回が締切）
                dates = re.findall(r"20\d\d\D{0,2}\d{1,2}\D{0,2}\d{1,2}", row_text)
                published = _normalize_date(dates[0]) if dates else ""
                deadline = _normalize_date(dates[-1]) if dates else ""

                tags = generate_tags(title, field_name, extra=[field_name])
                results.append({
                    "title": title,
                    "category": "プロポーザル",
                    "organization": "NEDO（新エネルギー・産業技術総合開発機構）",
                    "deadline": deadline,
                    "published_at": published,
                    "url": full_url,
                    "prefecture": "国",
                    "source": "NEDO",
                    "amount": "",
                    "summary": field_name,
                    "detail": "",
                    "tags": ",".join(tags),
                })

    logger.info(f"NEDO: {len(results)}件取得")
    return results


def fetch_nedo_detail(url: str) -> Dict[str, str]:
    """NEDO詳細ページを同期取得し、概要と締切を返す（詳細表示時の遅延取得用）。"""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            ct = resp.headers.get("Content-Type", "")
    except Exception as e:
        logger.error(f"詳細取得失敗 {url}: {e}")
        return {}
    soup = BeautifulSoup(_decode(raw, ct), "html.parser")
    text = soup.get_text("\n", strip=True)
    return {
        "detail": _extract_overview(soup),
        "deadline": _extract_deadline(text),
    }


# ---------------------------------------------------------------------------
# フォールバック用サンプル
# ---------------------------------------------------------------------------
SAMPLE_DATA = [
    {
        "title": "令和7年度 情報システム最適化業務委託",
        "category": "プロポーザル",
        "organization": "デジタル庁",
        "deadline": "2026-07-15",
        "published_at": "2026-06-20",
        "url": "https://www.digital.go.jp/procurement/",
        "prefecture": "国",
        "source": "デジタル庁",
        "amount": "5,000万円",
        "summary": "行政デジタル化推進のための情報システム最適化に係る業務委託",
        "detail": "行政のデジタル化推進に向け、情報システムの最適化に関する設計・構築・運用支援を行う業務委託です。",
        "tags": "AI・データ,委託",
    },
    {
        "title": "国道○○号道路改良工事",
        "category": "入札",
        "organization": "国土交通省 関東地方整備局",
        "deadline": "2026-07-10",
        "published_at": "2026-06-18",
        "url": "https://www.ktr.mlit.go.jp/",
        "prefecture": "国",
        "source": "国土交通省",
        "amount": "1億2,000万円",
        "summary": "一般競争入札（電子）",
        "detail": "国道の道路改良工事に関する一般競争入札（電子入札）です。",
        "tags": "",
    },
]


async def run_all_scrapers() -> List[Dict]:
    """全スクレイパーを実行してデータを返す。"""
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

    if not all_results:
        logger.info("スクレイピング結果が0件のためサンプルデータを使用")
        all_results.extend(SAMPLE_DATA)

    logger.info(f"合計 {len(all_results)}件")
    return all_results
