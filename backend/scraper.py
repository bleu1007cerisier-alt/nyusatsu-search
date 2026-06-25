"""
スクレイパー：主要官公庁の入札・プロポーザル情報を収集する
フェーズ1対象：
  - 調達ポータル（chotatsu.gpo.go.jp）
  - e-Gov 電子調達（一部）
  - 国土交通省
"""

import aiohttp
import asyncio
from bs4 import BeautifulSoup
from datetime import datetime
from typing import List, Dict
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NyusatsuSearch/1.0; +https://nyusatsu-search.example.com/bot)"
}


async def fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    try:
        await asyncio.sleep(2)  # サーバー負荷対策：2秒待機
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            return await resp.text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.error(f"取得失敗 {url}: {e}")
        return ""


async def scrape_chotatsu_portal() -> List[Dict]:
    """調達ポータル（GPO）から入札公告を取得"""
    results = []
    url = "https://www.chotatsu.gpo.go.jp/ppi/eppi031Action.do"

    async with aiohttp.ClientSession() as session:
        html = await fetch_html(session, url)
        if not html:
            return results

        soup = BeautifulSoup(html, "lxml")
        rows = soup.select("table.listTable tr")

        for row in rows[1:]:  # ヘッダー行をスキップ
            cols = row.find_all("td")
            if len(cols) < 4:
                continue
            link = cols[0].find("a")
            results.append({
                "title": link.text.strip() if link else cols[0].text.strip(),
                "category": "入札",
                "organization": cols[1].text.strip() if len(cols) > 1 else "",
                "deadline": cols[2].text.strip() if len(cols) > 2 else "",
                "published_at": cols[3].text.strip() if len(cols) > 3 else "",
                "url": "https://www.chotatsu.gpo.go.jp" + link["href"] if link and link.get("href") else url,
                "prefecture": "国",
                "source": "調達ポータル",
                "amount": "",
                "summary": "",
            })

    logger.info(f"調達ポータル: {len(results)}件取得")
    return results


async def scrape_mlit() -> List[Dict]:
    """国土交通省の入札公告を取得"""
    results = []
    url = "https://www.mlit.go.jp/chotatsu/index.html"

    async with aiohttp.ClientSession() as session:
        html = await fetch_html(session, url)
        if not html:
            return results

        soup = BeautifulSoup(html, "lxml")
        for a in soup.select("div.content a"):
            title = a.text.strip()
            href = a.get("href", "")
            if not title or len(title) < 5:
                continue
            if any(kw in title for kw in ["入札", "公募", "プロポーザル", "調達", "競争"]):
                full_url = href if href.startswith("http") else "https://www.mlit.go.jp" + href
                results.append({
                    "title": title,
                    "category": "プロポーザル" if "プロポーザル" in title or "公募" in title else "入札",
                    "organization": "国土交通省",
                    "deadline": "",
                    "published_at": "",
                    "url": full_url,
                    "prefecture": "国",
                    "source": "国土交通省",
                    "amount": "",
                    "summary": "",
                })

    logger.info(f"国土交通省: {len(results)}件取得")
    return results


async def scrape_soumu() -> List[Dict]:
    """総務省の調達情報を取得"""
    results = []
    url = "https://www.soumu.go.jp/menu_sinsei/cyoutatsu/index.html"

    async with aiohttp.ClientSession() as session:
        html = await fetch_html(session, url)
        if not html:
            return results

        soup = BeautifulSoup(html, "lxml")
        for a in soup.select("div#contentsArea a, div.contents a"):
            title = a.text.strip()
            href = a.get("href", "")
            if not title or len(title) < 5:
                continue
            if any(kw in title for kw in ["入札", "公募", "プロポーザル", "調達", "競争", "委託"]):
                full_url = href if href.startswith("http") else "https://www.soumu.go.jp" + href
                results.append({
                    "title": title,
                    "category": "プロポーザル" if "プロポーザル" in title or "公募" in title else "入札",
                    "organization": "総務省",
                    "deadline": "",
                    "published_at": "",
                    "url": full_url,
                    "prefecture": "国",
                    "source": "総務省",
                    "amount": "",
                    "summary": "",
                })

    logger.info(f"総務省: {len(results)}件取得")
    return results


# デモ用サンプルデータ（スクレイピングが失敗した場合のフォールバック）
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
        "title": "令和7年度 地域おこし協力隊支援業務",
        "category": "プロポーザル",
        "organization": "総務省",
        "deadline": "2026-07-20",
        "published_at": "2026-06-22",
        "url": "https://www.soumu.go.jp/menu_sinsei/cyoutatsu/",
        "prefecture": "国",
        "source": "総務省",
        "amount": "800万円",
        "summary": "地域おこし協力隊の活動支援・研修等に係る業務",
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
    {
        "title": "大阪府 庁舎清掃業務委託",
        "category": "入札",
        "organization": "大阪府 総務部",
        "deadline": "2026-07-08",
        "published_at": "2026-06-17",
        "url": "https://www.pref.osaka.lg.jp/",
        "prefecture": "大阪府",
        "source": "大阪府",
        "amount": "500万円",
        "summary": "一般競争入札",
    },
    {
        "title": "愛知県 産業振興計画策定支援業務",
        "category": "プロポーザル",
        "organization": "愛知県 経済産業局",
        "deadline": "2026-07-25",
        "published_at": "2026-06-24",
        "url": "https://www.pref.aichi.jp/",
        "prefecture": "愛知県",
        "source": "愛知県",
        "amount": "1,500万円",
        "summary": "次期産業振興計画の策定支援及び調査分析業務",
    },
    {
        "title": "文部科学省 教育ICT推進事業委託",
        "category": "プロポーザル",
        "organization": "文部科学省",
        "deadline": "2026-07-30",
        "published_at": "2026-06-25",
        "url": "https://www.mext.go.jp/",
        "prefecture": "国",
        "source": "文部科学省",
        "amount": "2,000万円",
        "summary": "GIGAスクール構想推進のためのICT活用支援事業",
    },
    {
        "title": "北海道 除雪業務委託（道央圏）",
        "category": "入札",
        "organization": "北海道 建設部",
        "deadline": "2026-09-01",
        "published_at": "2026-06-25",
        "url": "https://www.pref.hokkaido.lg.jp/",
        "prefecture": "北海道",
        "source": "北海道",
        "amount": "8,000万円",
        "summary": "令和7年度冬期道路除雪業務（一般競争入札）",
    },
]


async def run_all_scrapers() -> List[Dict]:
    """全スクレイパーを実行してデータを返す"""
    all_results = []

    tasks = [
        scrape_chotatsu_portal(),
        scrape_mlit(),
        scrape_soumu(),
    ]

    scraped = await asyncio.gather(*tasks, return_exceptions=True)

    for result in scraped:
        if isinstance(result, list):
            all_results.extend(result)

    # スクレイピング結果が少ない場合はサンプルデータを追加
    if len(all_results) < 3:
        logger.info("スクレイピング結果が少ないためサンプルデータを使用")
        all_results.extend(SAMPLE_DATA)

    return all_results
