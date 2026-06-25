"""
データ蓄積スクリプト（スクレイピングはここでのみ実行する）。

処理:
  1. NEDO等をスクレイピング
  2. 既存 dataset/tenders.csv を読み込み
  3. URLをキーにマージ（更新＝最新化、新規＝追加。既存は消さずに蓄積する）
  4. 概要(detail)が未取得のものは詳細ページから取得
  5. dataset/tenders.csv に書き出す

Webサイト側はこのCSVを読むだけでスクレイピングしない（軽量）。
GitHub Actions で定期実行し、更新があればCSVをコミットする。
"""

import os
import sys
import csv
import time
import asyncio
from datetime import date

# backend をインポートできるようにする
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from scraper import run_all_scrapers, fetch_nedo_detail, fetch_nedo_result  # noqa: E402

DATASET_DIR = os.path.join(ROOT, "dataset")
CSV_PATH = os.path.join(DATASET_DIR, "tenders.csv")

FIELDNAMES = [
    "id", "title", "category", "organization", "prefecture",
    "published_at", "deadline", "result_date", "project_code", "awardee",
    "amount", "url", "summary", "detail", "tags", "source",
    "first_seen", "last_seen",
]

# 1回の実行で詳細/結果ページを取得する最大件数（負荷・実行時間対策。未取得分を順次埋める）
MAX_DETAIL_PER_RUN = 200
DETAIL_SLEEP = 0.4


def _row_key(row: dict) -> str:
    """マージ用の一意キー。URLがあればURL、無ければ主要項目の組み合わせ。"""
    url = (row.get("url") or "").strip()
    if url:
        return "u:" + url
    return "k:" + "|".join([
        row.get("title", ""), row.get("published_at", ""),
        row.get("deadline", ""), row.get("result_date", ""),
    ])


def load_existing() -> dict:
    if not os.path.exists(CSV_PATH):
        return {}
    out = {}
    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            out[_row_key(row)] = row
    return out


def main():
    os.makedirs(DATASET_DIR, exist_ok=True)
    today = date.today().isoformat()

    existing = load_existing()
    print(f"既存データ: {len(existing)}件")

    scraped = asyncio.run(run_all_scrapers())
    print(f"スクレイピング取得: {len(scraped)}件")
    if not scraped:
        print("取得0件のため既存データを保持して終了")
        return

    # 既存IDの最大値（新規採番用）
    max_id = 0
    for r in existing.values():
        try:
            max_id = max(max_id, int(r.get("id") or 0))
        except ValueError:
            pass

    merged = dict(existing)  # key -> row

    new_count = 0
    update_count = 0
    for item in scraped:
        key = _row_key(item)
        if key in merged:
            prev = merged[key]
            # 最新情報で更新（締切・結果・タグ等）。detailは既存を維持（後段で補完）
            prev.update({
                "title": item.get("title", prev.get("title", "")),
                "category": item.get("category", prev.get("category", "")),
                "organization": item.get("organization", prev.get("organization", "")),
                "prefecture": item.get("prefecture", prev.get("prefecture", "")),
                "published_at": item.get("published_at") or prev.get("published_at", ""),
                "deadline": item.get("deadline") or prev.get("deadline", ""),
                "result_date": item.get("result_date") or prev.get("result_date", ""),
                "project_code": item.get("project_code") or prev.get("project_code", ""),
                "amount": item.get("amount") or prev.get("amount", ""),
                "summary": item.get("summary") or prev.get("summary", ""),
                "tags": item.get("tags") or prev.get("tags", ""),
                "source": item.get("source", prev.get("source", "")),
                "last_seen": today,
            })
            update_count += 1
        else:
            max_id += 1
            row = {k: item.get(k, "") for k in FIELDNAMES}
            row["id"] = str(max_id)
            row["first_seen"] = today
            row["last_seen"] = today
            merged[key] = row
            new_count += 1

    print(f"新規: {new_count}件 / 更新: {update_count}件 / 合計: {len(merged)}件")

    # 概要(detail)・予算規模(amount)が未取得のものを補完（NEDOのみ・URLあり）
    targets = [
        r for r in merged.values()
        if r.get("source") == "NEDO" and r.get("url") and not (r.get("detail") or "").strip()
    ]
    print(f"概要を取得する件数: {min(len(targets), MAX_DETAIL_PER_RUN)} / 未取得 {len(targets)}件")
    for r in targets[:MAX_DETAIL_PER_RUN]:
        info = fetch_nedo_detail(r["url"])
        if info.get("detail"):
            r["detail"] = info["detail"]
        if info.get("budget") and not (r.get("amount") or "").strip():
            r["amount"] = info["budget"]
        time.sleep(DETAIL_SLEEP)

    # 決定事業者(awardee)を補完：結果日があり、結果ページURLが取れる案件のみ
    aw_count = 0
    for item in scraped:
        if aw_count >= MAX_DETAIL_PER_RUN:
            break
        if not item.get("result_date") or not item.get("result_url"):
            continue
        row = merged.get(_row_key(item))
        if not row or (row.get("awardee") or "").strip():
            continue
        info = fetch_nedo_result(item["result_url"])
        if info.get("awardee"):
            row["awardee"] = info["awardee"]
        aw_count += 1
        time.sleep(DETAIL_SLEEP)
    print(f"決定事業者を取得した件数: {aw_count}")

    # ID順に並べて書き出し
    rows = sorted(merged.values(), key=lambda r: int(r.get("id") or 0))
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in FIELDNAMES})

    print(f"書き出し完了: {CSV_PATH} ({len(rows)}件)")


if __name__ == "__main__":
    main()
