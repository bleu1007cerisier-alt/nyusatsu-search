"""JST スクレイパーの動作確認スクリプト（ローカル実行用）。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import asyncio
from scraper import scrape_jst, fetch_jst_detail

def main():
    results = asyncio.run(scrape_jst())
    print(f"取得件数: {len(results)}")
    for r in results[:8]:
        print(f"  [{r['deadline']}] {r['title'][:45]} | 掲載:{r['published_at']} | {r['url'][:70]}")

    if results:
        print("\n--- 詳細取得テスト（1件目） ---")
        detail = fetch_jst_detail(results[0]["url"])
        print(f"  概要: {(detail.get('detail') or '')[:120]}")
        print(f"  予算: {detail.get('budget')}")
        print(f"  予定: {detail.get('schedule')}")
        print(f"  添付: {len(detail.get('attachments', []))}件")

if __name__ == "__main__":
    main()
