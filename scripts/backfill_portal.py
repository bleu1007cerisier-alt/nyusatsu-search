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

DATASET_DIR   = os.path.join(ROOT, "dataset")
CSV_PATH      = os.path.join(DATASET_DIR, "tenders.csv")
PROGRESS_PATH = os.path.join(DATASET_DIR, "portal_backfill_progress.json")

BACKFILL_START = date(2025, 4, 1)
DETAIL_SLEEP   = 1.2

_AI_USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0}


def _ai_summary(raw_text: str, title: str = "") -> str:
    """Claude Haiku で概要テキストを要約する。"""
    import os as _os
    api_key = _os.environ.get("ANTHROPIC_API_KEY") or _os.environ.get("CLAUDE_API_KEY")
    if not api_key or len(raw_text.strip()) < 80:
        return ""
    prompt = (
        "以下の入札・公募情報を日本語で3〜5文に要約してください。"
        "事業の目的、対象、予算規模、締切などの重要情報を含めてください。\n\n"
        f"タイトル: {title}\n\nテキスト:\n{raw_text[:8000]}\n\n要約文:"
    )
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        _AI_USAGE["calls"] += 1
        _AI_USAGE["input_tokens"] += int(getattr(msg.usage, "input_tokens", 0) or 0)
        _AI_USAGE["output_tokens"] += int(getattr(msg.usage, "output_tokens", 0) or 0)
        return msg.content[0].text.strip()[:600]
    except Exception as e:
        print(f"  AI要約失敗: {e}")
        return ""


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


def _fetch_detail(r: dict) -> bool:
    """詳細取得してrを更新。成功したらTrueを返す。"""
    if not r.get("url"):
        return False
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
            r["tags"] = ",".join(generate_tags(r.get("title", ""), r.get("source_category", "")))
            # AI要約（detailがあり未要約の場合）
            detail = (r.get("detail") or "").strip()
            if detail and len(detail) > 100 and not (r.get("summary") or "").strip():
                summary = _ai_summary(detail, r.get("title", ""))
                if summary:
                    r["summary"] = summary
            return True
    except Exception as e:
        print(f"  詳細取得失敗 {r.get('url', '')}: {e}")
    return False


def _months_to_process(prog: dict) -> list[tuple[date, date, str]]:
    """未処理の週リストを返す。"""
    completed = set(prog.get("completed_months", []))

    # バックフィル終了日を初回に固定
    if "backfill_end_date" not in prog:
        _, rows = _load_csv()
        portal_dates = [r["published_at"] for r in rows
                        if r.get("source") == "PORTAL" and r.get("published_at")
                        and r["published_at"] >= "2026-06-01"]
        earliest_daily = min(portal_dates) if portal_dates else date.today().isoformat()
        prog["backfill_end_date"] = (date.fromisoformat(earliest_daily) - timedelta(days=1)).isoformat()
        _save_progress(prog)

    end_date = date.fromisoformat(prog["backfill_end_date"])
    months = []
    cur = BACKFILL_START.replace(day=1)
    while cur <= end_date:
        last_day = monthrange(cur.year, cur.month)[1]
        month_end = min(cur.replace(day=last_day), end_date)
        key = cur.strftime("%Y-%m")
        if key not in completed:
            week_start = cur
            while week_start <= month_end:
                week_end = min(week_start + timedelta(days=6), month_end)
                months.append((week_start, week_end, key))
                week_start = week_end + timedelta(days=1)
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    prog = _load_progress()
    fieldnames, rows = _load_csv()

    # --- フェーズ1: 既存CSVの未取得PORTAL案件に詳細を補完 ---
    unfetched = [r for r in rows
                 if r.get("source") == "PORTAL" and r.get("budget_checked", "") != "1"]
    if unfetched:
        print(f"フェーズ1: 既存未取得 {len(unfetched)}件の詳細を補完")
        for i, r in enumerate(unfetched, 1):
            _fetch_detail(r)
            time.sleep(DETAIL_SLEEP)
            if i % 50 == 0:
                _save_csv(fieldnames, rows)
                print(f"  {i}/{len(unfetched)}件処理済み")
        _save_csv(fieldnames, rows)
        print(f"フェーズ1完了")

    # --- フェーズ2: 未取得月の新規レコードを追加 ---
    months = _months_to_process(prog)
    if not months:
        print("フェーズ2: 補完対象の月はありません。")
        return

    print(f"\nフェーズ2: {len(months)}週分を取得 ({months[0][0].strftime('%Y/%m/%d')} 〜 {months[-1][1].strftime('%Y/%m/%d')})")

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

        new_items = [r for r in scraped if r.get("project_code") not in existing_case_nos]
        print(f"  取得: {len(scraped)}件 / 新規: {len(new_items)}件")

        # 詳細取得してからCSVに追加
        for r in new_items:
            _fetch_detail(r)
            time.sleep(DETAIL_SLEEP)
            rows.append(r)
            existing_case_nos.add(r.get("project_code", ""))
            total_added += 1

        _save_csv(fieldnames, rows)

        # 月の最終週が終わったら完了済みに
        if (month_end + timedelta(days=1)).strftime("%Y-%m") != month_key:
            completed_month_keys.add(month_key)
            prog["completed_months"] = sorted(completed_month_keys)
        _save_progress(prog)
        print(f"  追加済み累計: {total_added}件")
        time.sleep(2)

    print(f"\n補完完了: 合計 {total_added}件追加")
    if _AI_USAGE["calls"]:
        cost = _AI_USAGE["input_tokens"] * 0.0000008 + _AI_USAGE["output_tokens"] * 0.000004
        print(f"AI要約: {_AI_USAGE['calls']}回 / 入力{_AI_USAGE['input_tokens']}tok 出力{_AI_USAGE['output_tokens']}tok / 推定${cost:.3f}")


if __name__ == "__main__":
    main()
