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

# ローカル実行時の .env 読み込み
_env_path = os.path.join(ROOT, ".env")
if os.path.exists(_env_path):
    for _line in open(_env_path, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from scraper import scrape_portal, fetch_portal_detail, generate_tags  # noqa: E402

DATASET_DIR   = os.path.join(ROOT, "dataset")
CSV_PATH      = os.path.join(DATASET_DIR, "tenders.csv")
PROGRESS_PATH = os.path.join(DATASET_DIR, "portal_backfill_progress.json")

BACKFILL_START = date(2025, 4, 1)
DETAIL_SLEEP   = 1.2

_AI_USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

_EXTRACT_PROMPT = """\
以下は入札公告・公募情報のテキストです。下記のJSON形式のみで出力してください（前後の説明文は不要）。

{
  "deadline": "YYYY-MM-DD または null",
  "amount": "金額文字列 または null",
  "schedule": [
    {"date": "YYYY-MM-DD", "label": "ラベル", "raw": "原文の日付表現"}
  ],
  "bullets": [
    "事業内容: ...",
    "履行場所: ...",
    "履行期間: ...",
    "落札方式: ...",
    "参加資格: ...",
    "予算規模: ...",
    "入札締切: ...",
    "開札予定: ...",
    "担当: ..."
  ]
}

【抽出ルール】
- deadline: 入札書提出期限・応募締切・提出期限の日付を YYYY-MM-DD に変換。令和7=2025, 令和8=2026, 令和9=2027, 令和10=2028。不明はnull
- amount: 予算上限額・概算額・契約上限額の文字列（例: "約1,200万円"）。不明はnull
- schedule: 説明会・仕様書配布・質問受付・提出期限・開札日時など日付のある予定を全て抽出。和暦→西暦変換
- bullets: 8〜12項目を「ラベル: 値」形式で。不明は「記載なし」。電話番号・メールアドレス不要
- 日本語の単語に英字を混入させない（GEPS/AI/IT等の一般的な略語のみ可）\
"""


def _ai_extract(raw_text: str, title: str = "") -> dict:
    """Claude Haiku で構造化情報を抽出する。"""
    import os as _os, json as _json, re as _re
    api_key = _os.environ.get("ANTHROPIC_API_KEY") or _os.environ.get("CLAUDE_API_KEY")
    if not api_key or len(raw_text.strip()) < 80:
        return {}
    prompt = _EXTRACT_PROMPT + f"\n\nタイトル: {title}\n\nテキスト:\n{raw_text[:8000]}"
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        for attempt in range(3):
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1200,
                messages=[{"role": "user", "content": prompt}],
            )
            _AI_USAGE["calls"] += 1
            _AI_USAGE["input_tokens"] += int(getattr(msg.usage, "input_tokens", 0) or 0)
            _AI_USAGE["output_tokens"] += int(getattr(msg.usage, "output_tokens", 0) or 0)
            text = msg.content[0].text.strip()
            if "```" in text:
                m = _re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
                if m:
                    text = m.group(1).strip()
            try:
                data = _json.loads(text)
                return data
            except Exception:
                if attempt < 2:
                    continue
        return {}
    except Exception as e:
        print(f"  AI抽出失敗: {e}")
        return {}


def _bullets_to_summary(bullets: list) -> str:
    return "\n".join(f"・{b}" for b in bullets if b)


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
            # AI抽出（detailがあり未処理の場合）
            detail = (r.get("detail") or "").strip()
            if detail and len(detail) > 100 and not (r.get("summary") or "").strip():
                extracted = _ai_extract(detail, r.get("title", ""))
                if extracted.get("bullets"):
                    r["summary"] = _bullets_to_summary(extracted["bullets"])
                if extracted.get("deadline"):
                    r["deadline"] = extracted["deadline"]
                if extracted.get("amount") and not (r.get("amount") or "").strip():
                    r["amount"] = extracted["amount"]
                if extracted.get("schedule"):
                    import json as _json
                    r["schedule"] = _json.dumps(extracted["schedule"], ensure_ascii=False)
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

    # --- フェーズ0: 全PORTAL案件のsummaryを新形式（箇条書き）で再生成 ---
    # 旧形式（段落テキスト）と未生成の両方を対象にする
    def _needs_regen(r):
        if r.get("source") != "PORTAL":
            return False
        det = (r.get("detail") or "").strip()
        if len(det) < 100:
            return False
        summ = (r.get("summary") or "").strip()
        # 未生成 or 旧形式（・で始まらない段落テキスト）
        return not summ or not summ.startswith("・")

    needs_regen = [r for r in rows if _needs_regen(r)]
    if needs_regen:
        print(f"フェーズ0: AI再抽出対象 {len(needs_regen)}件（新形式への移行 + 未生成）")
        for i, r in enumerate(needs_regen, 1):
            extracted = _ai_extract((r.get("detail") or "").strip(), r.get("title", ""))
            if extracted.get("bullets"):
                r["summary"] = _bullets_to_summary(extracted["bullets"])
            if extracted.get("deadline"):
                r["deadline"] = extracted["deadline"]
            if extracted.get("amount") and not (r.get("amount") or "").strip():
                r["amount"] = extracted["amount"]
            if extracted.get("schedule") and not (r.get("schedule") or "").strip():
                import json as _json
                r["schedule"] = _json.dumps(extracted["schedule"], ensure_ascii=False)
            if i % 100 == 0:
                _save_csv(fieldnames, rows)
                print(f"  {i}/{len(needs_regen)}件処理済み")
        _save_csv(fieldnames, rows)
        print("フェーズ0完了")
    else:
        print("フェーズ0: 対象レコードなし（全件新形式済み）")

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
