"""
PORTAL案件の締切日を再取得して修正するスクリプト。

公開終了日（掲載終了日）を誤って締切として保存していた問題を修正する。
入札書提出期限・応募期限などを正しく取得し直す。
"""

import os, sys, csv, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

# .env 読み込み
_env_path = os.path.join(ROOT, ".env")
if os.path.exists(_env_path):
    for _line in open(_env_path, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from scraper import fetch_portal_detail  # noqa

CSV_PATH = os.path.join(ROOT, "dataset", "tenders.csv")
SLEEP = 1.5
SAVE_INTERVAL = 50

def main():
    sys.stdout.reconfigure(encoding="utf-8")

    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    portal_rows = [r for r in rows if r.get("source") == "PORTAL"]
    print(f"PORTAL案件: {len(portal_rows)}件")

    updated = 0
    failed = 0

    for i, r in enumerate(portal_rows, 1):
        url = (r.get("url") or "").strip()
        if not url:
            continue

        try:
            info = fetch_portal_detail(url)
        except Exception as e:
            print(f"  エラー: {e}")
            failed += 1
            time.sleep(SLEEP)
            continue

        new_dl = (info.get("deadline") or "").strip()
        old_dl = (r.get("deadline") or "").strip()

        if new_dl and new_dl != old_dl:
            r["deadline"] = new_dl
            updated += 1
            if updated <= 5 or updated % 100 == 0:
                print(f"  更新: {r.get('title','')[:40]} {old_dl} → {new_dl}")

        if i % SAVE_INTERVAL == 0:
            _save(fieldnames, rows)
            print(f"  {i}/{len(portal_rows)}件処理済み (更新{updated}件)")

        time.sleep(SLEEP)

    _save(fieldnames, rows)
    print(f"\n完了: {len(portal_rows)}件処理 / {updated}件更新 / {failed}件エラー")

def _save(fieldnames, rows):
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

if __name__ == "__main__":
    main()
