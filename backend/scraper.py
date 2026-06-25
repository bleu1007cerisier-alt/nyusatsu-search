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
        await asyncio.sleep(1.2)  # サーバー負荷対策
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


async def _scrape_nedo_detail(session: aiohttp.ClientSession, item: Dict) -> None:
    """NEDO詳細ページを取得して item に deadline / detail / tags を補完する（破壊的更新）。"""
    raw, ct = await fetch_bytes(session, item["url"])
    if not raw:
        return
    soup = BeautifulSoup(_decode(raw, ct), "html.parser")
    text = soup.get_text("\n", strip=True)

    deadline = _extract_deadline(text)
    if deadline:
        item["deadline"] = deadline

    overview = _extract_overview(soup)
    if overview:
        item["detail"] = overview

    # タイトル＋概要＋分野からタグを生成
    field = item.get("summary", "")
    tags = generate_tags(item.get("title", ""), overview, field, extra=[field] if field else None)
    item["tags"] = ",".join(tags)


async def scrape_nedo() -> List[Dict]:
    """NEDO 公募情報一覧＋各詳細を取得する。"""
    results: List[Dict] = []
    url = "https://www.nedo.go.jp/koubo/index.html"
    base = "https://www.nedo.go.jp"

    async with aiohttp.ClientSession() as session:
        raw, ct = await fetch_bytes(session, url)
        if not raw:
            return results
        soup = BeautifulSoup(_decode(raw, ct), "html.parser")

        seen = set()
        for a in soup.find_all("a", href=re.compile(r"^/koubo/.*\.html$")):
            li = a.find_parent("li")
            if not li:
                continue
            full_text = li.get_text(" ", strip=True)
            if len(full_text) < 12:
                continue

            href = a.get("href", "")
            link_text = a.get_text(strip=True)

            m = re.match(
                r"(20\d\d年\d{1,2}月\d{1,2}日)\s*(\S+)?\s*(本公募|公募予告|予告|公募|決定|採択)?\s*(.*)",
                full_text,
            )
            if not m:
                continue
            date_raw, field, status, _ = m.groups()
            field = field or ""
            status = status or ""

            title = link_text or (m.group(4) or "").strip()
            if not title or len(title) < 6:
                continue

            # 「結果・決定」は応募できる案件ではないため除外
            if status in ("決定", "採択") or "の決定について" in title or "実施体制の決定" in title or "採択" in title:
                continue

            full_url = base + href
            if full_url in seen:
                continue
            seen.add(full_url)

            # 分野名（NEDOの内部カテゴリ）を概要欄に保持
            summary_field = field
            results.append({
                "title": title,
                "category": "プロポーザル",
                "organization": "NEDO（新エネルギー・産業技術総合開発機構）",
                "deadline": "",
                "published_at": _normalize_date(date_raw),
                "url": full_url,
                "prefecture": "国",
                "source": "NEDO",
                "amount": "",
                "summary": summary_field,
                "detail": "",
                "tags": "",
                "_status": status,
            })

        # 詳細ページを取得して締切・概要・タグを補完
        targets = results[:MAX_DETAIL_FETCH]
        for item in targets:
            await _scrape_nedo_detail(session, item)

    # タグがまだ空のもの（詳細取得失敗等）はタイトルだけでタグ生成
    for item in results:
        if not item.get("tags"):
            item["tags"] = ",".join(
                generate_tags(item.get("title", ""), item.get("summary", ""),
                              extra=[item["summary"]] if item.get("summary") else None)
            )
        # 状態（本公募/予告）を概要の先頭に付与
        st = item.pop("_status", "")
        if st and st not in item["summary"]:
            item["summary"] = (st + " / " + item["summary"]).strip(" /")

    logger.info(f"NEDO: {len(results)}件取得（詳細取得 {len(results[:MAX_DETAIL_FETCH])}件）")
    return results


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
