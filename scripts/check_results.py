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
    fetch_jogmec_result_url, fetch_jogmec_result, fetch_aichi_results,
    fetch_osaka_result, fetch_fukuoka_results,
    fetch_mie_results, fetch_niigata_results, fetch_ishikawa_results,
    fetch_toyama_award, fetch_gifu_award,
)
import re as _re

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

    # 愛知県（入札）の決定事業者：入札結果を一括取得し orderNum で突合する。
    aichi_need = any(
        r.get("source") == "AICHI" and not (r.get("awardee") or "").strip()
        and (r.get("awardee_checked") or "") != "1" and "orderNum=" in (r.get("url") or "")
        for r in rows)
    aichi_results = fetch_aichi_results() if aichi_need else {}

    # 福岡県の決定事業者：「落札者の公示」記事を一括取得し、案件名で突合する
    # （福岡県は入札公告と結果公示が別記事でIDの紐づけが無いため、愛知県と同じ一括方式）
    fukuoka_need = any(
        r.get("source") == "FUKUOKA" and not (r.get("awardee") or "").strip()
        and (r.get("awardee_checked") or "") != "1"
        for r in rows)
    fukuoka_results = fetch_fukuoka_results() if fukuoka_need else {}

    # 「（入札中止）」等が付く案件は不成立で終わっているため、同名の別回（再公告）が
    # 落札した場合に誤って紐づいてしまう。案件名だけの一致では判別できないため、
    # 中止・不調を示す案件は突合対象から除外する。
    _FUKUOKA_VOID = _re.compile(r"入札中止|不調|不落|中止|取消|取り止め")

    def _fukuoka_match(title):
        if _FUKUOKA_VOID.search(title):
            return None
        norm = _re.sub(r"[\s　]", "", title)
        for name, rec in fukuoka_results.items():
            if norm in name or name in norm:
                return rec
        return None

    # 三重・新潟・石川：結果記事は公告と別記事（またはID非連動）のため、
    # 一括取得した結果一覧とタイトルのbigram類似度で突合する
    mie_need = any(
        r.get("source") == "MIE" and not (r.get("awardee") or "").strip()
        and (r.get("awardee_checked") or "") != "1" for r in rows)
    mie_results = fetch_mie_results() if mie_need else []

    niigata_need = any(
        r.get("source") == "NIIGATA" and not (r.get("awardee") or "").strip()
        and (r.get("awardee_checked") or "") != "1" for r in rows)
    niigata_results = fetch_niigata_results() if niigata_need else []

    ishikawa_need = any(
        r.get("source") == "ISHIKAWA" and not (r.get("awardee") or "").strip()
        and (r.get("awardee_checked") or "") != "1" for r in rows)
    ishikawa_results = fetch_ishikawa_results() if ishikawa_need else []

    def _title_bigrams(s):
        s = _re.sub(r"[\s　]", "", s or "")
        return {s[i:i + 2] for i in range(len(s) - 1)}

    def _bigram_match(title, candidates, threshold=0.5):
        bg = _title_bigrams(title)
        if not bg:
            return None
        best, score = None, 0.0
        for rec in candidates:
            cbg = rec.get("bigrams") or set()
            if not cbg:
                continue
            inter = len(bg & cbg)
            if not inter:
                continue
            j = inter / len(bg | cbg)
            if j > score:
                best, score = rec, j
        return best if score >= threshold else None

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

        # 愛知県：一括取得済みの結果マップから突合（ネットワークは1回だけ・件数上限の対象外）
        if src == "AICHI":
            on = _re.search(r"orderNum=([0-9]+)", row.get("url", ""))
            rec = aichi_results.get(on.group(1)) if on else None
            if rec:
                row["awardee"] = _ai_split_awardee(rec["awardee"])
                row["awardee_checked"] = "1"
                if rec.get("result_date") and not (row.get("result_date") or "").strip():
                    row["result_date"] = rec["result_date"]
                if rec.get("amount") and not (row.get("amount") or "").strip():
                    row["amount"] = rec["amount"]
                updated += 1
            continue

        # 福岡県：一括取得済みの「落札者の公示」記事から案件名で突合（ネットワークは1回だけ）
        if src == "FUKUOKA":
            rec = _fukuoka_match(row.get("title", ""))
            if rec and rec.get("awardee"):
                row["awardee"] = _ai_split_awardee(rec["awardee"])
                row["awardee_checked"] = "1"
                if rec.get("result_date") and not (row.get("result_date") or "").strip():
                    row["result_date"] = rec["result_date"]
                if rec.get("amount") and not (row.get("amount") or "").strip():
                    row["amount"] = rec["amount"]
                updated += 1
            continue

        # 三重・新潟・石川：一括取得済みの結果一覧とタイトルのbigram類似度で突合
        if src in ("MIE", "NIIGATA", "ISHIKAWA"):
            candidates = {"MIE": mie_results, "NIIGATA": niigata_results,
                          "ISHIKAWA": ishikawa_results}[src]
            rec = _bigram_match(row.get("title", ""), candidates)
            if rec and rec.get("awardee"):
                row["awardee"] = _ai_split_awardee(rec["awardee"])
                row["awardee_checked"] = "1"
                if rec.get("result_date") and not (row.get("result_date") or "").strip():
                    row["result_date"] = rec["result_date"]
                if rec.get("amount") and not (row.get("amount") or "").strip():
                    row["amount"] = rec["amount"]
                updated += 1
            continue

        if checked >= MAX_CHECK_PER_RUN:
            continue

        result_url = (row.get("result_url") or "").strip()
        info = {}

        if result_url:
            # result_url がある場合は直接取得（NEDO・PORTAL・大阪府）
            if src == "PORTAL":
                info = fetch_portal_award(result_url)
            elif src == "NEDO":
                result_date = (row.get("result_date") or "").strip()
                if result_date:
                    info = fetch_nedo_result(result_url)
            elif src == "OSAKA":
                # 入札締切前は未開札のため無駄打ちを避ける
                deadline = (row.get("deadline") or "").strip()
                if not deadline or deadline <= str(today):
                    info = fetch_osaka_result(result_url)
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

        elif src == "TOYAMA":
            # 富山県：タイトル・本文が同一URL上で更新されるため、そのURLを再取得するだけでよい
            page_url = (row.get("url") or "").strip()
            if page_url:
                info = fetch_toyama_award(page_url) or {}
                checked += 1
                time.sleep(DETAIL_SLEEP)

        elif src == "GIFU":
            # 岐阜県：同上。決定事業者は「選定結果」PDF添付から取得する
            page_url = (row.get("url") or "").strip()
            if page_url:
                info = fetch_gifu_award(page_url) or {}
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
