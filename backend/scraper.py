"""
スクレイパー：官公庁・公的機関の入札・公募情報を収集する。

設計方針:
  - 文字コードは自動判定（日本語の官公庁サイトは Shift_JIS が多い）
  - 静的HTMLで一覧を公開している、確実に取得できるソースのみを対象とする
  - 取得に失敗した場合のみサンプルデータにフォールバックする

現在の対象:
  - NEDO（新エネルギー・産業技術総合開発機構）公募情報
"""

import aiohttp
import asyncio
import re
from bs4 import BeautifulSoup
from typing import List, Dict
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NyusatsuSearch/1.0; +https://nyusatsu-search.onrender.com/)"
}


def _decode(raw: bytes, content_type: str = "") -> str:
    """バイト列を適切な文字コードでデコードする（Shift_JIS / UTF-8 / EUC-JP を自動判定）。"""
    # 1) HTTPヘッダのcharset
    ct = (content_type or "").lower()
    # 2) HTML先頭のmeta charset
    head = raw[:3000].decode("ascii", "ignore").lower()
    blob = ct + " " + head
    if "shift_jis" in blob or "shift-jis" in blob or "x-sjis" in blob:
        enc = "cp932"  # cp932 は Shift_JIS の上位互換
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
        await asyncio.sleep(1.5)  # サーバー負荷対策
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            raw = await resp.read()
            return raw, resp.headers.get("Content-Type", "")
    except Exception as e:
        logger.error(f"取得失敗 {url}: {e}")
        return b"", ""


def _normalize_date(text: str) -> str:
    """'2026年6月25日' や '2026/6/25' を 'YYYY-MM-DD' に正規化する。"""
    m = re.search(r"(20\d\d)\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})", text)
    if not m:
        return ""
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    return f"{y}-{mo:02d}-{d:02d}"


async def scrape_nedo() -> List[Dict]:
    """NEDO 公募情報一覧を取得する。

    一覧は各案件が <li> に「日付 分野 状態 タイトル」を持ち、
    リンクは /koubo/XXXX.html 形式。
    """
    results: List[Dict] = []
    url = "https://www.nedo.go.jp/koubo/index.html"
    base = "https://www.nedo.go.jp"

    async with aiohttp.ClientSession() as session:
        raw, ct = await fetch_bytes(session, url)
        if not raw:
            return results
        html = _decode(raw, ct)
        soup = BeautifulSoup(html, "html.parser")

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

            # 「日付 分野 状態 …」を分解
            m = re.match(
                r"(20\d\d年\d{1,2}月\d{1,2}日)\s*(\S+)?\s*(本公募|公募予告|予告|公募|決定|採択)?\s*(.*)",
                full_text,
            )
            if not m:
                continue
            date_raw, field, status, _ = m.groups()
            field = field or ""
            status = status or ""

            # タイトルはリンクのテキストを優先（より正確）
            title = link_text or (m.group(4) or "").strip()
            if not title or len(title) < 6:
                continue

            # 公募の「結果・決定」は応募できる案件ではないため除外
            if status in ("決定", "採択") or "の決定について" in title or "実施体制の決定" in title or "採択" in title:
                continue

            full_url = base + href
            if full_url in seen:
                continue
            seen.add(full_url)

            summary_parts = [p for p in [field, status] if p]
            results.append({
                "title": title,
                "category": "プロポーザル",  # NEDOは公募（プロポーザル型）
                "organization": "NEDO（新エネルギー・産業技術総合開発機構）",
                "deadline": "",  # 一覧には締切が無い（詳細ページ参照）
                "published_at": _normalize_date(date_raw),
                "url": full_url,
                "prefecture": "国",
                "source": "NEDO",
                "amount": "",
                "summary": " / ".join(summary_parts),
            })

    logger.info(f"NEDO: {len(results)}件取得")
    return results


# デモ用サンプルデータ（全スクレイピングが失敗した場合のフォールバック）
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
    },
    {
        "title": "東京都 DX推進コンサルティング業務",
        "category": "プロポーザル",
        "organization": "東京都 デジタルサービス局",
        "deadline": "2026-07-05",
        "published_at": "2026-06-15",
        "url": "https://www.digitalservice.metro.tokyo.lg.jp/",
        "prefecture": "東京都",
        "source": "東京都",
        "amount": "3,000万円",
        "summary": "都庁内DX推進に向けたコンサルティング及び伴走支援",
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

    # 実データが1件も取れなかった場合のみサンプルデータを使用
    if not all_results:
        logger.info("スクレイピング結果が0件のためサンプルデータを使用")
        all_results.extend(SAMPLE_DATA)

    logger.info(f"合計 {len(all_results)}件")
    return all_results
