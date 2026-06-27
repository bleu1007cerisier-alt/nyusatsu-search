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
import re as _re_summary
import time
import asyncio
from datetime import date


_PHONE_RE = _re_summary.compile(r'\d{2,5}[-－ ]\d{1,4}[-－ ]\d{3,4}')
_EMAIL_RE_OUT = _re_summary.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')


def _ai_summary(raw_text: str, title: str = "") -> str:
    """Claude Haiku で入札公告・公募テキストを要約する。
    ANTHROPIC_API_KEY が未設定の場合は空文字を返す（生テキストをそのまま使用）。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    if not api_key or len(raw_text.strip()) < 80:
        return ""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        prompt = (
            "以下の入札公告・公募・研究開発事業のテキストを300〜500文字程度の自然な日本語で要約してください。\n\n"
            "【必須ルール】\n"
            "・「要約」「概要」「#」などの見出しやラベルは一切つけない\n"
            "・業務名・タイトルの繰り返しは不要\n"
            "・電話番号・メールアドレスは含めない\n"
            "・箇条書きは使わず、読みやすい連続した文章にする\n\n"
            "【できる限り含める情報】\n"
            "・調達・業務・研究の目的と具体的な内容\n"
            "・履行期間・事業期間\n"
            "・競争参加資格・応募要件\n"
            "・入札・開札・応募締切の日程\n"
            "・予算規模・上限額\n\n"
            f"タイトル: {title}\n\n"
            f"テキスト:\n{raw_text[:8000]}\n\n"
            "要約文:"
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = msg.content[0].text.strip()
        # 電話番号・メールアドレスを後処理で除去
        summary = _PHONE_RE.sub('', summary)
        summary = _EMAIL_RE_OUT.sub('', summary)
        return summary[:600] if summary else ""
    except Exception as e:
        print(f"AI要約失敗: {e}")
        return ""


# backend をインポートできるようにする
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from scraper import (  # noqa: E402
    run_all_scrapers, fetch_nedo_detail, fetch_nedo_result,
    fetch_jst_detail, fetch_portal_detail, fetch_portal_award,
    _extract_pdf_budget,
)
from datetime import date, timedelta
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
    """添付PDFをR2へ保存し、保存先情報を row['attachments'] に記録する（R2有効時のみ）。

    GEPS(geps.go.jp)など認証必須リンクはPDFが返らないためR2スキップ。
    その場合でも source_url はメタデータとして保存し、UIでリンク表示に使う。
    """
    if not storage.r2_enabled():
        return  # 鍵未設定なら何もしない（attachments_checkedも立てず、有効化後に処理）
    import re as _re
    stored = []
    for i, att in enumerate(attachments):
        data = _download(att["url"])
        r2_url = ""
        r2_key = ""
        # PDFマジックナンバー確認（認証必要なURLはHTMLが返るためスキップ）
        if data and data.lstrip()[:4] == b"%PDF":
            safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", att["url"].split("/")[-1]) or f"file{i}.pdf"
            pub_date = (row.get("published_at") or "unknown").replace("/", "-")
            src_prefix = (row.get("source") or "misc").lower()
            key = f"{src_prefix}/{pub_date}_{row['id']}/{att['kind']}_{safe}"
            public = storage.upload_bytes(key, data, "application/pdf")
            r2_url = public if public.startswith("http") else ""
            r2_key = key
        # PDFでなくても source_url をメタデータとして保存（UIでリンク表示可能）
        stored.append({"name": att["name"], "kind": att["kind"],
                       "url": r2_url, "key": r2_key, "source_url": att["url"]})
    row["attachments"] = json.dumps(stored, ensure_ascii=False)
    row["attachments_checked"] = "1"

def _overview_from_r2(row: dict) -> str:
    """R2保存済みPDFから概要テキストを抽出する（detail未記入の案件のみ）。"""
    if not storage.r2_enabled():
        return ""
    try:
        atts = json.loads(row.get("attachments") or "[]")
    except Exception:
        return ""
    if not atts:
        return ""
    import io
    import boto3
    from pypdf import PdfReader
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ.get("R2_ENDPOINT", ""),
        aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID", ""),
        aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", ""),
    )
    bucket = os.environ.get("R2_BUCKET", "")
    # 公募要領→仕様書→調達資料の順に試行（意味ある文章が得られるまで）
    priority = ["公募要領", "仕様書", "調達資料", "審査基準", "評価基準"]
    atts_sorted = sorted(atts, key=lambda a: next(
        (i for i, k in enumerate(priority) if k == a.get("kind")), 99))
    for att in atts_sorted:
        key = att.get("key", "")
        if not key:
            continue
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            data = obj["Body"].read()
            reader = PdfReader(io.BytesIO(data))
            # 先頭5ページのテキストから意味ある行だけ結合
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:5])
            lines = [ln.strip() for ln in text.split("\n")
                     if ln.strip() and len(ln.strip()) > 8]
            if lines:
                return " ".join(lines[:15])[:500]
        except Exception as e:
            print(f"R2 PDF概要読込失敗 {key}: {e}")
    return ""


def _budget_from_r2(row: dict) -> str:
    """R2保存済みPDFから予算規模を抽出する（R2が有効で添付ファイルがある案件のみ）。"""
    if not storage.r2_enabled():
        return ""
    try:
        atts = json.loads(row.get("attachments") or "[]")
    except Exception:
        return ""
    if not atts:
        return ""
    import io
    import boto3
    from pypdf import PdfReader
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ.get("R2_ENDPOINT", ""),
        aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID", ""),
        aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", ""),
    )
    bucket = os.environ.get("R2_BUCKET", "")
    # 公募要領→仕様書→その他の順で試行
    priority = ["公募要領", "仕様書", "審査基準", "評価基準"]
    atts_sorted = sorted(atts, key=lambda a: next(
        (i for i, k in enumerate(priority) if k == a.get("kind")), 99))
    for att in atts_sorted:
        key = att.get("key", "")
        if not key:
            continue
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            data = obj["Body"].read()
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
            budget = _extract_pdf_budget(text)
            if budget:
                return budget
        except Exception as e:
            print(f"R2 PDF読込失敗 {key}: {e}")
    return ""


# 1回の実行で詳細/結果ページを取得する最大件数（負荷・実行時間対策。未取得分を順次埋める）
MAX_DETAIL_PER_RUN = 200
DETAIL_SLEEP = 0.4


import re as _re_key

_PORTAL_OLD_URL = _re_key.compile(r'[?&]id=(\d+)$')


def _normalize_url(url: str) -> str:
    """ポータルの旧URL形式(?id=xxx)を新形式に正規化（マージキー用）。"""
    if "p-portal.go.jp" in url:
        m = _PORTAL_OLD_URL.search(url)
        if m:
            return (
                "https://www.p-portal.go.jp/pps-web-biz/UAA01/OAA0104"
                f"?procurementItemInfoId={m.group(1)}"
            )
    return url


def _row_key(row: dict) -> str:
    """マージ用の一意キー。URLがあればURL（正規化済み）、無ければ主要項目の組み合わせ。"""
    url = (row.get("url") or "").strip()
    if url:
        return "u:" + _normalize_url(url)
    return "k:" + "|".join([
        row.get("title", ""), row.get("published_at", ""),
        row.get("deadline", ""), row.get("result_date", ""),
    ])


def _row_score(row: dict) -> tuple:
    """重複解決用スコア。大きいほどデータが豊富。"""
    return (
        int((row.get("budget_checked") or "") == "1"),
        int(bool((row.get("detail") or "").strip())),
        int(bool((row.get("amount") or "").strip())),
        -int(row.get("id") or 999999),  # IDが小さい（古い）ほど優先
    )


def load_existing() -> dict:
    if not os.path.exists(CSV_PATH):
        return {}
    out = {}
    dupe_count = 0
    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            # URL フィールド自体も正規化（旧形式 ?id= を消去）
            if "p-portal.go.jp" in (row.get("url") or ""):
                row["url"] = _normalize_url(row["url"])
            key = _row_key(row)
            if key in out:
                # 重複: スコアが高い方（データが豊富）を残す
                if _row_score(row) > _row_score(out[key]):
                    out[key] = row
                dupe_count += 1
            else:
                out[key] = row
    if dupe_count:
        print(f"重複レコード除去（CSV読込時）: {dupe_count}件")
    return out


def main():
    os.makedirs(DATASET_DIR, exist_ok=True)
    today = date.today().isoformat()

    existing = load_existing()
    print(f"既存データ: {len(existing)}件")

    # 調達ポータル: 既存CSVの最新PORTAL掲載日 − 1日 を取得開始日に使う
    # （前回実行時の最終掲載日の翌日以降だけを取得 → 重複なし・取りこぼしなし）
    portal_dates = [
        r.get("published_at", "")
        for r in existing.values()
        if r.get("source") == "PORTAL" and r.get("published_at")
    ]
    if portal_dates:
        last_portal = max(portal_dates)          # "YYYY-MM-DD"
        portal_from = (date.fromisoformat(last_portal) - timedelta(days=1)).strftime("%Y/%m/%d")
    else:
        portal_from = (date.today() - timedelta(days=7)).strftime("%Y/%m/%d")  # 初回のみ7日分
    print(f"調達ポータル取得開始日: {portal_from}")

    scraped = asyncio.run(run_all_scrapers(portal_date_from=portal_from))
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

    # 既存CSVタイトルから【事業コード】プレフィックスを除去（整合性維持）
    import re as _re
    _GARBAGE_DETAIL = _re.compile(r'^[口□・　\s]+$')  # 記号のみのゴミdetect

    for r in merged.values():
        if r.get("title"):
            r["title"] = _re.sub(r"^\s*【[^】]+】\s*", "", r["title"]).strip()

    # PORTAL: 旧URLフォーマット(?id=)を新形式(?procurementItemInfoId=)に移行
    url_migrated = 0
    for r in merged.values():
        if r.get("source") == "PORTAL" and r.get("url"):
            old_url = r["url"]
            if "?id=" in old_url and "procurementItemInfoId" not in old_url:
                item_id = old_url.split("?id=")[-1]
                r["url"] = f"https://www.p-portal.go.jp/pps-web-biz/UAA01/OAA0104?procurementItemInfoId={item_id}"
                url_migrated += 1
    if url_migrated:
        print(f"PORTAL: URL旧形式→新形式移行 {url_migrated}件")

    # 【増分】概要が未取得、または予算が未取得で未確認の案件だけ取得。
    # 本文に予算が無ければ公募要領PDFから補完。一度確認した案件は再取得しない。
    _FETCH_SOURCES = {"NEDO", "JST", "PORTAL"}

    # PORTAL: ゴミ記号・ヘッダーのみの detail をリセット（→ 再取得 & AI要約の対象に）。
    # 空の detail は「取得済みだが portal 側に情報がない」ため再取得しない（無限ループ防止）。
    _HEADER_ONLY = _re_key.compile(r'^[入　札公告\s　]{2,40}$')  # 「入　札　公　告」など
    portal_retry = 0
    for r in merged.values():
        if r.get("source") == "PORTAL" and (r.get("budget_checked") or "") == "1":
            det = (r.get("detail") or "").strip()
            is_garbage = det and _GARBAGE_DETAIL.match(det)
            is_header_only = det and _HEADER_ONLY.match(det)
            if is_garbage or is_header_only:
                r["budget_checked"] = ""
                r["detail"] = ""
                portal_retry += 1
    if portal_retry:
        print(f"PORTAL: ゴミ・ヘッダーのみdetail({portal_retry}件)を再取得対象にリセット")

    def needs_fetch(r):
        if r.get("source") not in _FETCH_SOURCES or not r.get("url"):
            return False
        # budget_checked=1 が「詳細取得を一度試みた」フラグ。立っていなければ必ず取得する
        if (r.get("budget_checked") or "") != "1":
            return True
        # R2有効で添付ファイルがまだ未保存の場合のみ再取得
        if storage.r2_enabled() and (r.get("attachments_checked") or "") != "1":
            return True
        return False

    targets = [r for r in merged.values() if needs_fetch(r)]
    print(f"概要/予算を取得（増分）: {min(len(targets), MAX_DETAIL_PER_RUN)}件")
    for r in targets[:MAX_DETAIL_PER_RUN]:
        src = r.get("source", "")
        if src == "JST":
            info = fetch_jst_detail(r["url"])
        elif src == "PORTAL":
            info = fetch_portal_detail(r["url"])
        else:
            info = fetch_nedo_detail(r["url"])  # 概要＋予算（本文→無ければPDF）＋予定
        if info:  # ページ取得成功
            new_detail = info.get("detail", "")
            cur_detail = (r.get("detail") or "").strip()
            # PORTAL はゴミdetailをリセット済みなので常に上書き。他ソースは空のときのみ
            if new_detail and (not cur_detail or r.get("source") == "PORTAL"):
                r["detail"] = new_detail  # 生テキストを保持
            # 長い公告テキスト（100文字超）はAI要約してsummaryフィールドへ
            if new_detail and len(new_detail) > 100:
                summarized = _ai_summary(new_detail, r.get("title", ""))
                if summarized:
                    r["summary"] = summarized
            if info.get("budget") and not (r.get("amount") or "").strip():
                r["amount"] = info["budget"]
            if info.get("schedule") and not (r.get("schedule") or "").strip():
                r["schedule"] = json.dumps(info["schedule"], ensure_ascii=False)
            # PORTAL: 調達種別→category / 公開終了日→deadline を上書き
            if src == "PORTAL":
                if info.get("category"):
                    r["category"] = info["category"]
                if info.get("deadline") and not (r.get("deadline") or "").strip():
                    r["deadline"] = info["deadline"]
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
        if (row.get("awardee") or "").strip() or (row.get("awardee_checked") or "").strip() == "1":
            continue
        if not item.get("result_url"):
            continue
        src_aw = row.get("source", "")
        if src_aw == "PORTAL":
            info = fetch_portal_award(item["result_url"])
        elif src_aw == "NEDO":
            if not (row.get("result_date") or "").strip():
                continue
            info = fetch_nedo_result(item["result_url"])
        else:
            continue
        if info:
            if info.get("awardee"):
                row["awardee"] = info["awardee"]
            if info.get("result_date") and not (row.get("result_date") or "").strip():
                row["result_date"] = info["result_date"]
            row["awardee_checked"] = "1"
            aw_count += 1
            time.sleep(DETAIL_SLEEP)
    print(f"決定事業者を確認（増分）: {aw_count}件")

    # R2保存済みPDFから予算を補完（添付あり・予算未取得の案件）
    r2_budget_count = 0
    for r in merged.values():
        if (r.get("amount") or "").strip():
            continue  # 既に予算あり
        if not (r.get("attachments") or "").strip():
            continue  # R2にPDFなし
        budget = _budget_from_r2(r)
        if budget:
            r["amount"] = budget
            r2_budget_count += 1
    print(f"R2 PDFから予算補完: {r2_budget_count}件")

    # R2保存済みPDFから概要を補完（添付あり・detail未記入の案件）
    r2_detail_count = 0
    for r in merged.values():
        if (r.get("detail") or "").strip():
            continue  # 既に概要あり
        if not (r.get("attachments") or "").strip():
            continue  # R2にPDFなし
        overview = _overview_from_r2(r)
        if overview:
            r["detail"] = overview
            r2_detail_count += 1
    print(f"R2 PDFから概要補完: {r2_detail_count}件")

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
