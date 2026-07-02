"""
NEDO / JST / JOGMEC の旧形式summary（#で始まる or 段落テキスト）を
箇条書き新形式に再生成するスクリプト。

実行方法:
  cd nyusatsu-search
  .venv\Scripts\python.exe scripts/backfill_summary.py
"""

import os, sys, csv, json, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "backend"))

# .env 読み込み
_env_path = os.path.join(ROOT, ".env")
if os.path.exists(_env_path):
    for _line in open(_env_path, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from build_dataset import _ai_extract, _bullets_to_summary, _AI_USAGE

CSV_PATH = os.path.join(ROOT, "dataset", "tenders.csv")
TARGETS = {"NEDO", "JST", "JOGMEC"}
SLEEP = 0.5
SAVE_INTERVAL = 50


def _needs_regen(r):
    if r.get("source") not in TARGETS:
        return False
    det = (r.get("detail") or "").strip()
    if len(det) < 100:
        return False
    summ = (r.get("summary") or "").strip()
    # 未生成 or 旧形式（・で始まらない）
    return not summ or not summ.startswith("・")


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    targets = [r for r in rows if _needs_regen(r)]
    by_src = {}
    for r in targets:
        s = r.get("source", "")
        by_src[s] = by_src.get(s, 0) + 1
    print(f"対象: {len(targets)}件 {by_src}")

    if not targets:
        print("対象なし。終了します。")
        return

    updated = 0
    for i, r in enumerate(targets, 1):
        det = (r.get("detail") or "").strip()
        extracted = _ai_extract(det, r.get("title", ""))
        if extracted.get("bullets"):
            r["summary"] = _bullets_to_summary(extracted["bullets"])
            updated += 1
        if extracted.get("amount") and not (r.get("amount") or "").strip():
            r["amount"] = extracted["amount"]
        if extracted.get("schedule") and not (r.get("schedule") or "").strip():
            r["schedule"] = json.dumps(extracted["schedule"], ensure_ascii=False)

        if i % SAVE_INTERVAL == 0:
            _save(fieldnames, rows)
            cost = _AI_USAGE["input_tokens"] * 0.0000008 + _AI_USAGE["output_tokens"] * 0.000004
            print(f"  {i}/{len(targets)}件処理 / 更新{updated}件 / 推定${cost:.3f}")
        time.sleep(SLEEP)

    _save(fieldnames, rows)
    cost = _AI_USAGE["input_tokens"] * 0.0000008 + _AI_USAGE["output_tokens"] * 0.000004
    print(f"\n完了: {len(targets)}件処理 / {updated}件更新 / 推定${cost:.3f}")


def _save(fieldnames, rows):
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
