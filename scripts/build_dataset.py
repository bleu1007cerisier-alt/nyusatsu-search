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
import json
import time
import asyncio
from datetime import date

# backend をインポートできるようにする
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from scraper import (  # noqa: E402
    run_all_scrapers, fetch_nedo_detail, fetch_nedo_result,
)
import storage  # noqa: E402

DATASET_DIR = os.path.join(ROOT, "dataset")
CSV_PATH = os.path.join(DATASET_DIR, "tenders.csv")

FIELDNAMES = [
    "id", "title", "category", "organization", "prefecture",
    "published_at", "deadline", "result_date", "project_code", "awardee",
    "awardee_checked", "amount", "budget_checked", "url", "summary", "detail",
    "schedule", "attachments", "attachments_checked", "tags", "source",
    "first_seen", "last_seen",
]


def _download(url: str) -> bytes:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except Exception as e:  # noqa: BLE001
        print(f"添付DL失敗 {url}: {e}")
        return b""


def _store_attachments(row, attachments):
    """添付PDFをR2へ保存し、保存先情報を row['attachments'] に記録する（R2有効時のみ）。"""
    if not storage.r2_enabled():
        return  # 鍵未設定なら何もしない（attachments_checkedも立てず、有効化後に処理）
    import re as _re
    stored = []
    for i, att in enumerate(attachments):
        data = _download(att["url"])
        if not data:
            continue
        safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", att["url"].split("/")[-1]) or f"file{i}.pdf"
        pub_date = (row.get("published_at") or "unknown").replace("/", "-")
        key = f"nedo/{pub_date}_{row['id']}/{att['kind']}_{safe}"
        public = storage.upload_bytes(key, data, "application/pdf")
        # 公開URL（http...）のときだけ表示用urlに採用。非公開保存時は原文リンクにフォールバック
        url = public if public.startswith("http") else ""
        stored.append({"name": att["name"], "kind": att["kind"],
                       "url": url, "key": key, "source_url": att["url"]})
    row["attachments"] = json.dumps(stored, ensure_ascii=False)
    row["attachments_checked"] = "1"

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

    # 【増分】概要が未取得、または予算が未取得で未確認の案件だけ取得。
    # 本文に予算が無ければ公募要領PDFから補完。一度確認した案件は再取得しない。
    def needs_fetch(r):
        if r.get("source") != "NEDO" or not r.get("url"):
            return False
        if not (r.get("detail") or "").strip():
            return True  # 概要未取得（新規等）
        if not (r.get("amount") or "").strip() and (r.get("budget_checked") or "") != "1":
            return True  # 予算未取得かつ未確認（取りこぼしの補完）
        if storage.r2_enabled() and (r.get("attachments_checked") or "") != "1":
            return True  # 添付ファイル未保存（R2有効時）
        return False

    targets = [r for r in merged.values() if needs_fetch(r)]
    print(f"概要/予算を取得（増分）: {min(len(targets), MAX_DETAIL_PER_RUN)}件")
    for r in targets[:MAX_DETAIL_PER_RUN]:
        info = fetch_nedo_detail(r["url"])  # 概要＋予算（本文→無ければPDF）＋予定
        if info:  # ページ取得成功
            if info.get("detail") and not (r.get("detail") or "").strip():
                r["detail"] = info["detail"]
            if info.get("budget") and not (r.get("amount") or "").strip():
                r["amount"] = info["budget"]
            if info.get("schedule") and not (r.get("schedule") or "").strip():
                r["schedule"] = json.dumps(info["schedule"], ensure_ascii=False)
            r["budget_checked"] = "1"  # 予算確認済み（空でも再取得しない）
            # 添付ファイル（仕様書・公募要領・評価基準）をR2へ保存（有効時のみ・未保存のみ）
            if storage.r2_enabled() and (r.get("attachments_checked") or "") != "1":
                _store_attachments(r, info.get("attachments", []))
        time.sleep(DETAIL_SLEEP)

    # 【増分】決定事業者：結果が出ていて未チェックの案件だけ確認（一度確認したら再取得しない）
    aw_count = 0
    for item in scraped:
        if aw_count >= MAX_DETAIL_PER_RUN:
            break
        row = merged.get(_row_key(item))
        if not row:
            continue
        if not (row.get("result_date") or "").strip():
            continue
        if (row.get("awardee") or "").strip() or (row.get("awardee_checked") or "").strip() == "1":
            continue
        if not item.get("result_url"):
            continue
        info = fetch_nedo_result(item["result_url"])
        if info:  # ページ取得成功（社名が無くても確認済みにして再取得を防ぐ）
            if info.get("awardee"):
                row["awardee"] = info["awardee"]
            row["awardee_checked"] = "1"
            aw_count += 1
            time.sleep(DETAIL_SLEEP)
    print(f"決定事業者を確認（増分）: {aw_count}件")

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
