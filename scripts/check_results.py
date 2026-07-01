"""
事業者決定チェックスクリプト（週2回実行）

処理:
  1. dataset/tenders.csv を読み込み
  2. 以下の案件を対象に結果を確認する
     - awardee が空 かつ awardee_checked != "1"
     - result_url がある場合 → 結果ページを直接取得（NEDO/PORTAL）
     - JST案件で result_url がない場合 → 公募ページを再取得して採択リンクを探す
  3. published_at から1年超で awardee が空 → awardee_checked="1" で監視終了
  4. 更新があれば dataset/tenders.csv に書き出す

GitHub Actions で水・金 17:30 JST に実行。
build_dataset.py（毎朝09:30）とは別ジョブとして分離することで、
スクレイピングと結果確認の負荷を分散する。
"""

import os
import sys
import csv
import time
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from scraper import (  # noqa: E402
    fetch_nedo_result, fetch_portal_award, fetch_jst_detail,
    fetch_jogmec_result_url, fetch_jogmec_result,
)

DATASET_DIR = os.path.join(ROOT, "dataset")
CSV_PATH = os.path.join(DATASET_DIR, "tenders.csv")

# 1年以上前の公募は監視終了とみなす
MONITOR_EXPIRE_DAYS = 365

# 1回の実行で確認する最大件数（負荷対策）
MAX_CHECK_PER_RUN = 50

DETAIL_SLEEP = 1.5


def _ai_split_awardee(awardee: str) -> str:
    """複数社が連結された事業者名をAIで分割し、'｜'区切りで返す。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    if not api_key or "｜" in awardee or len(awardee) < 20:
        return awardee
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        prompt = (
            "以下のテキストは、複数の会社・法人名が区切り文字なしで連結されたものです。\n"
            "各法人名を正確に分割して、'｜'（全角パイプ）で区切って出力してください。\n"
            "1社だけの場合はそのまま出力してください。\n"
            "法人名以外のテキスト（説明文・記号・改行など）は含めないでください。\n\n"
            f"入力: {awardee}\n"
            "出力:"
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip() or awardee
    except Exception as e:
        print(f"AI事業者分割失敗: {e}")
        return awardee


def main():
    # CSV読み込み
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    today = date.today()
    expire_threshold = today - timedelta(days=MONITOR_EXPIRE_DAYS)

    updated = 0
    expired = 0
    checked = 0

    for row in rows:
        # 既に事業者確定 or 監視終了 → スキップ
        if (row.get("awardee") or "").strip():
            continue
        if (row.get("awardee_checked") or "") == "1":
            continue

        src = row.get("source", "")
        pub = row.get("published_at", "")

        # 1年超で監視終了
        if pub and pub < str(expire_threshold):
            row["awardee_checked"] = "1"
            expired += 1
            continue

        if checked >= MAX_CHECK_PER_RUN:
            continue

        result_url = (row.get("result_url") or "").strip()
        info = {}

        if result_url:
            # result_url がある場合は直接取得（NEDO・PORTAL）
            if src == "PORTAL":
                info = fetch_portal_award(result_url)
            elif src == "NEDO":
                result_date = (row.get("result_date") or "").strip()
                if result_date:
                    info = fetch_nedo_result(result_url)
            checked += 1
            time.sleep(DETAIL_SLEEP)

        elif src == "JST":
            # JST: 公募ページを再取得して採択リンクを探す
            page_url = (row.get("url") or "").strip()
            if page_url:
                detail = fetch_jst_detail(page_url)
                found_result_url = (detail.get("result_url") or "").strip()
                if found_result_url:
                    row["result_url"] = found_result_url
                    print(f"JST採択リンク発見: {found_result_url}")
                    # 見つかったページからさらに事業者を取得（次回実行で対応）
                checked += 1
                time.sleep(DETAIL_SLEEP)

        elif src == "JOGMEC":
            # JOGMEC: 公募ページを再取得して「結果」PDFリンクを探し、PDFから抽出
            page_url = (row.get("url") or "").strip()
            if page_url:
                pdf_url = fetch_jogmec_result_url(page_url)
                if pdf_url:
                    row["result_url"] = pdf_url
                    info = fetch_jogmec_result(pdf_url)
                    time.sleep(DETAIL_SLEEP)
                checked += 1
                time.sleep(DETAIL_SLEEP)

        if info.get("awardee"):
            row["awardee"] = _ai_split_awardee(info["awardee"])
            row["awardee_checked"] = "1"
            if info.get("result_date") and not (row.get("result_date") or "").strip():
                row["result_date"] = info["result_date"]
            updated += 1
            print(f"事業者決定: {row.get('title','')[:40]} → {row['awardee'][:40]}")

    print(f"事業者確認: {checked}件チェック / {updated}件更新 / {expired}件監視終了")

    # ログ書き出し（更新がなくても記録する）
    _write_result_log(checked, updated, expired)

    if updated + expired == 0:
        print("更新なし")
        return

    # CSV書き出し
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print("CSV保存完了")


def _write_result_log(checked: int, updated: int, expired: int):
    """事業者チェックの実行ログを dataset/check_results_log.json に追記する（直近50件）。"""
    import json
    from datetime import datetime, timezone
    log_path = os.path.join(DATASET_DIR, "check_results_log.json")
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checked": checked,
        "updated": updated,
        "expired": expired,
    }
    history = []
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                history = data.get("runs", []) if isinstance(data, dict) else []
        except (ValueError, OSError):
            history = []
    history.append(entry)
    history = history[-50:]
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump({"runs": history}, f, ensure_ascii=False, indent=2)
    print(f"結果チェックログ更新: {checked}件確認 / {updated}件更新")


if __name__ == "__main__":
    main()
