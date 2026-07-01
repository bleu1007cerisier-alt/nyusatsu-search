"""
PORTAL 過去データ補完スクリプト

2025年4月以降のPORTAL公募を月単位で取得し、既存CSVに追加する。
毎日の build_dataset.py とは独立して手動実行する想定。

実行方法:
  cd nyusatsu-search
  python scripts/backfill_portal.py

進捗は dataset/portal_backfill_progress.json に保存され、
途中で止めても再開できる。
"""

import os, sys, csv, json, asyncio, time
from datetime import date, timedelta
from calendar import monthrange

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from scraper import scrape_portal, fetch_portal_detail, generate_tags  # noqa: E402

DATASET_DIR = os.path.join(ROOT, "dataset")
CSV_PATH    = os.path.join(DATASET_DIR, "tenders.csv")
PROGRESS_PATH = os.path.join(DATASET_DIR, "portal_backfill_progress.json")

BACKFILL_START = date(2025, 4, 1)   # 補完開始日
DETAIL_SLEEP   = 1.2                # 詳細取得間隔（秒）
MAX_DETAIL_PER_RUN = 200            # 1実行あたりの詳細取得上限


def _load_progress() -> dict:
    if os.path.exists(PROGRESS_PATH):
        with open(PROGRESS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"completed_months": [], "last_run": ""}


def _save_progress(prog: dict):
    prog["last_run"] = date.today().isoformat()
    with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False, indent=2)


def _load_csv() -> tuple[list, list]:
    """CSVを読み込み (fieldnames, rows) を返す。"""
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        rows = list(reader)
    return fieldnames, rows


def _save_csv(fieldnames: list, rows: list):
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _months_to_process(prog: dict) -> list[tuple[date, date]]:
    """補完開始日から現在の最初のPORTALデータ直前まで、未処理の月リストを返す。"""
    completed = set(prog.get("completed_months", []))
    # バックフィル終了日（日々スクレイピング開始前日）を初回に記録して固定
    if "backfill_end_date" not in prog:
        _, rows = _load_csv()
        portal_dates = [r["published_at"] for r in rows
                        if r.get("source") == "PORTAL" and r.get("published_at")
                        and r["published_at"] >= "2026-06-01"]  # 直近の日次データのみ
        earliest_daily = min(portal_dates) if portal_dates else date.today().isoformat()
        prog["backfill_end_date"] = (date.fromisoformat(earliest_daily) - timedelta(days=1)).isoformat()
        _save_progress(prog)
    end_date = date.fromisoformat(prog["backfill_end_date"])

    # 月単位で区切るが、500件上限回避のため件数が多い月は週単位に自動分割
    months = []
    cur = BACKFILL_START.replace(day=1)
    while cur <= end_date:
        last_day = monthrange(cur.year, cur.month)[1]
        month_end = min(cur.replace(day=last_day), end_date)
        key = cur.strftime("%Y-%m")
        if key not in completed:
            # 週単位（7日）に分割して登録
            week_start = cur
            while week_start <= month_end:
                week_end = min(week_start + timedelta(days=6), month_end)
                months.append((week_start, week_end, key))
                week_start = week_end + timedelta(days=1)
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


def main():
    prog = _load_progress()
    months = _months_to_process(prog)

    if not months:
        print("補完対象の月がありません。全月取得済みか、既にカバー済みです。")
        return

    print(f"補完対象: {len(months)}週分 ({months[0][0].strftime('%Y/%m/%d')} 〜 {months[-1][1].strftime('%Y/%m/%d')})")

    fieldnames, rows = _load_csv()
    existing_case_nos = {r["project_code"] for r in rows if r.get("project_code")}
    total_added = 0
    completed_month_keys = set(prog.get("completed_months", []))

    for month_start, month_end, month_key in months:
        date_from = month_start.strftime("%Y/%m/%d")
        date_to   = month_end.strftime("%Y/%m/%d")
        print(f"\n--- {date_from} 〜 {date_to} ---")

        try:
            scraped = asyncio.run(scrape_portal(date_from=date_from, date_to=date_to))
        except Exception as e:
            print(f"  取得エラー: {e}")
            time.sleep(5)
            continue

        # 重複除外
        new_items = [r for r in scraped if r.get("project_code") not in existing_case_nos]
        print(f"  取得: {len(scraped)}件 / 新規: {len(new_items)}件")

        if not new_items:
            prog.setdefault("completed_months", []).append(month_start.strftime("%Y-%m"))
            _save_progress(prog)
            continue

        # 詳細取得（概要・締切・種別）
        detail_count = 0
        for r in new_items:
            if detail_count >= MAX_DETAIL_PER_RUN:
                print(f"  詳細取得上限({MAX_DETAIL_PER_RUN}件)に達したため残りは次回実行")
                break
            if not r.get("url"):
                continue
            try:
                info = fetch_portal_detail(r["url"])
                if info:
                    if info.get("detail"):
                        r["detail"] = info["detail"]
                    if info.get("deadline"):
                        r["deadline"] = info["deadline"]
                    if info.get("category"):
                        r["category"] = info["category"]
                    if info.get("source_category"):
                        r["source_category"] = info["source_category"]
                    if info.get("amount"):
                        r["amount"] = info["amount"]
                    if info.get("attachments"):
                        r["attachments"] = json.dumps(info["attachments"], ensure_ascii=False)
                    r["budget_checked"] = "1"
                    # タグ再生成（種別が確定してから）
                    r["tags"] = ",".join(generate_tags(r.get("title",""), r.get("source_category","")))
            except Exception as e:
                print(f"  詳細取得失敗 {r.get('url','')}: {e}")
            detail_count += 1
            time.sleep(DETAIL_SLEEP)

        # CSVに追加
        for r in new_items:
            if r.get("project_code") not in existing_case_nos:
                rows.append(r)
                existing_case_nos.add(r.get("project_code",""))
                total_added += 1

        _save_csv(fieldnames, rows)
        # 月の最終週が終わったら月キーを完了済みに
        next_day = month_end + timedelta(days=1)
        if next_day.strftime("%Y-%m") != month_key:
            completed_month_keys.add(month_key)
            prog["completed_months"] = sorted(completed_month_keys)
        _save_progress(prog)
        print(f"  追加済み累計: {total_added}件")
        time.sleep(2)

    print(f"\n補完完了: 合計 {total_added}件追加")


if __name__ == "__main__":
    main()
