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


_PHONE_RE = _re_summary.compile(r'\(?\d{2,5}\)?[-－ ．]?\d{1,4}[-－ ．]\d{3,4}')
_EMAIL_RE_OUT = _re_summary.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
# 先頭のマークダウン見出し行（「# タイトル」「# ○○の要約」等）を行ごと除去
_HEADER_PREFIX = _re_summary.compile(r'^\s*[#＃]+[^\n]*\n+')
# 電話・メール除去後に残る空（中身が記号・空白・電話ラベルのみ）の括弧を除去
_EMPTY_PAREN = _re_summary.compile(
    r'[（(][\s　、。:：・ー―\-]*(?:TEL|FAX|電話|℡|内線)?[\s　:：．.\-－]*[)）]',
    _re_summary.IGNORECASE)


# 自治体CMSの定型スキップ文言（本文と無関係なので detail から除去）
_DETAIL_BOILERPLATE = _re_summary.compile(
    r'(?:ここから本文です。?|本文ここまで。?|このページの先頭へ(?:戻る)?。?)')


def _strip_detail_boilerplate(s: str) -> str:
    """『ここから本文です。』等のCMS定型文を detail 本文から除去する。"""
    if not s:
        return s
    s = _DETAIL_BOILERPLATE.sub('', s)
    s = _re_summary.sub(r'^[\s　]+', '', s)   # 先頭の空白・改行を詰める
    return s.strip()


def _clean_summary(s: str) -> str:
    """AI要約の後処理：見出し・電話番号・メール・空括弧を除去し整形する。"""
    if not s:
        return ""
    s = _HEADER_PREFIX.sub('', s)            # 先頭見出し行を除去
    s = _EMAIL_RE_OUT.sub('', s)             # メールアドレス
    s = _PHONE_RE.sub('', s)                 # 電話番号
    s = _EMPTY_PAREN.sub('', s)              # 空になった括弧
    s = _re_summary.sub(r'[ 　]{2,}', ' ', s)   # 連続スペースを1つに
    s = _re_summary.sub(r'\n{3,}', '\n\n', s)   # 連続改行を詰める
    return s.strip()


# 日本語の単語の途中に英字が混入する Haiku の誤生成を検出（「万ha」=ヘクタール等は除外）
_LATIN_IN_JP = _re_summary.compile(r'[ぁ-んァ-ヶ一-鿿][a-z]{2,}[ぁ-んァ-ヶ一-鿿]')
_LATIN_OK = {"ha"}  # 日本語に隣接しても許容する英字（単位・略語）


def _has_corrupt_latin(s: str) -> bool:
    """日本語の単語内に英字が紛れ込んだ誤生成があれば True。"""
    for m in _LATIN_IN_JP.findall(s or ""):
        if m[1:-1].lower() not in _LATIN_OK:
            return True
    return False


# AI使用量の累積（1回の実行内）。開発ページのコスト推定に使う。
_AI_USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
# Claude Haiku 4.5 の料金（USD / 100万トークン）https://www.anthropic.com/pricing
_HAIKU_IN_PER_M = 0.80
_HAIKU_OUT_PER_M = 4.00


_EXTRACT_PROMPT = """\
以下は入札公告・公募情報のテキストです（「【添付資料抜粋】」以降は仕様書・公募要領などの本文です）。
下記のJSON形式のみで出力してください（前後の説明文は不要）。

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
- 事業内容: 「その業務で実際にやること」を具体的に記述する（この項目は最重要。詳しめに）。
  公告本文や仕様書・公募要領の抜粋から、①業務の目的・背景、②具体的な作業内容・業務範囲、
  ③対象（場所・施設・システム・数量等）、④成果物・納品物、⑤履行期間の要点を読み取り、
  3〜5文で「何を・どこで・どこまでやる業務か」が具体的に分かるようにまとめる。
  例:「大阪湾の藻場再生に向け、企業・団体の新規参入を支援する事業。参入希望者への相談対応・
  技術的助言、藻場創出の試行区画の設定とモニタリング支援を行い、成果を報告書にまとめる。
  履行期間は契約日から令和9年3月まで。」のように作業の中身が伝わる粒度で書く。
  タイトルをそのまま繰り返すだけにしない。挨拶・募集告知等の定型の前置きは省くが、
  業務内容そのものは端折らず具体的に述べる。
  ※添付資料が用語集・マニュアル等で案件と無関係な場合は無視し、タイトルと公告本文から推定して記述する。
  「本文に記載がありません」等のメタ的な文は書かず、必ず業務内容そのものを述べる。
- bullets: 「ラベル: 値」形式。情報が本文から読み取れない項目は、その行を出力しない（「記載なし」とは書かない）。
  事業内容の行は必ず出力する。電話番号・メールアドレスは含めない
- 日本語の単語に英字を混入させない（GEPS/AI/IT等の一般的な略語のみ可）\
"""


def _ai_extract(raw_text: str, title: str = "") -> dict:
    """Claude Haiku でテキストから構造化情報を抽出する。
    返り値: {"deadline": str|None, "amount": str|None, "schedule": list, "bullets": list}
    APIキー未設定・短いテキストは空dictを返す。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    if not api_key or len(raw_text.strip()) < 80:
        return {}
    prompt = (
        _EXTRACT_PROMPT
        + f"\n\nタイトル: {title}\n\nテキスト:\n{raw_text[:8000]}"
    )
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        for attempt in range(3):
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1200,
                messages=[{"role": "user", "content": prompt}],
            )
            try:
                _AI_USAGE["calls"] += 1
                _AI_USAGE["input_tokens"] += int(getattr(msg.usage, "input_tokens", 0) or 0)
                _AI_USAGE["output_tokens"] += int(getattr(msg.usage, "output_tokens", 0) or 0)
            except Exception:
                pass
            text = msg.content[0].text.strip()
            # JSON部分だけ抽出（```json ... ``` ブロックも対応）
            if "```" in text:
                import re as _re
                m = _re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
                if m:
                    text = m.group(1).strip()
            try:
                data = json.loads(text)
                if isinstance(data, list):  # 稀に配列で返るケースの救済
                    data = data[0] if data and isinstance(data[0], dict) else {}
                if not isinstance(data, dict):
                    raise ValueError("unexpected JSON shape")
            except Exception:
                if attempt < 2:
                    continue
                return {}
            bullets = data.get("bullets") or []
            # 英字混入チェック（問題なければ採用）
            summary_text = "\n".join(bullets)
            if not _has_corrupt_latin(summary_text):
                return data
        return {}
    except Exception as e:
        print(f"AI抽出失敗: {e}")
        return {}


# 値が「情報なし」を意味する箇条書きを判定（除外用）
_NOINFO_VAL = _re_summary.compile(
    r'^(記載なし|記載無し|なし|無し|無|null|None|N/?A|不明|未定|―+|-+|－+|該当なし)?\s*$',
    _re_summary.IGNORECASE)


def _is_noinfo_bullet(b: str) -> bool:
    """『・履行場所: 記載なし』のような情報のない箇条書きなら True。"""
    body = str(b).strip().lstrip("・").strip()
    if "：" in body:
        val = body.split("：", 1)[1]
    elif ":" in body:
        val = body.split(":", 1)[1]
    else:
        return False  # ラベルのみ（値なし）は判定対象外（そのまま残す）
    return bool(_NOINFO_VAL.match(val.strip()))


def _bullets_to_summary(bullets: list) -> str:
    """箇条書きリストを summary フィールド用の文字列に変換。
    値が「記載なし」等の情報のない項目は除外する。"""
    return "\n".join(f"・{b}" for b in bullets if b and not _is_noinfo_bullet(b))


def _ai_split_awardee(awardee: str) -> str:
    """複数社が連結された事業者名をAIで分割し、'｜'区切りで返す。
    1社だけの場合はそのまま返す。失敗時は元の文字列を返す。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    if not api_key:
        return awardee
    # 区切り文字がすでにある・短い場合はスキップ
    if "｜" in awardee or len(awardee) < 10:
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
        result = msg.content[0].text.strip()
        _AI_USAGE["calls"] += 1
        _AI_USAGE["input_tokens"] += msg.usage.input_tokens
        _AI_USAGE["output_tokens"] += msg.usage.output_tokens
        return result if result else awardee
    except Exception as e:
        print(f"AI事業者分割失敗: {e}")
        return awardee


# backend をインポートできるようにする
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from scraper import (  # noqa: E402
    run_all_scrapers, fetch_nedo_detail, fetch_nedo_result,
    fetch_jst_detail, fetch_portal_detail, fetch_portal_award,
    fetch_jogmec_detail, fetch_aichi_detail, fetch_tokyo_detail, _extract_pdf_budget,
    fetch_osaka_detail, fetch_osaka_proposal_detail, fetch_fukuoka_detail,
    fetch_mie_detail, fetch_gifu_detail, fetch_yamanashi_detail, fetch_toyama_detail,
    fetch_nagano_detail, fetch_shizuoka_detail, fetch_fukui_detail, fetch_niigata_detail,
    fetch_tochigi_detail, fetch_chiba_detail, fetch_kyoto_detail, fetch_hyogo_detail, fetch_shiga_detail, fetch_wakayama_detail, fetch_hiroshima_detail, fetch_okayama_detail, fetch_ehime_detail, fetch_kochi_detail, fetch_saga_detail, fetch_shimane_detail, fetch_kumamoto_detail, fetch_hokkaido_detail, fetch_tokushima_detail, fetch_nagasaki_detail, fetch_okinawa_detail, fetch_oita_detail, fetch_akita_detail, fetch_fukushima_detail, fetch_tottori_detail, fetch_gunma_detail,
)
from datetime import date, timedelta
import storage  # noqa: E402

DATASET_DIR = os.path.join(ROOT, "dataset")
CSV_PATH = os.path.join(DATASET_DIR, "tenders.csv")

FIELDNAMES = [
    "id", "title", "category", "organization", "prefecture",
    "published_at", "deadline", "close_date", "result_date", "project_code", "awardee",
    "awardee_checked", "amount", "budget_checked", "url", "result_url",
    "source_category", "summary", "detail",
    "schedule", "attachments", "attachments_checked", "tags", "source",
    "first_seen", "last_seen",
]


def _safe_url(url: str) -> str:
    """URLのパス・クエリに未エスケープの日本語等が含まれる場合にパーセントエンコードする。

    東京都サイト等、リンクの href に日本語ファイル名をそのまま埋め込んでいる
    ケースがあり、urllib はASCII外の文字を送信できずエラーになるため。
    """
    try:
        url.encode("ascii")
        return url  # 既にASCIIのみ→エンコード不要
    except UnicodeEncodeError:
        from urllib.parse import urlsplit, urlunsplit, quote
        parts = urlsplit(url)
        path = quote(parts.path, safe="/%")
        query = quote(parts.query, safe="=&%")
        return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def _download(url: str) -> bytes:
    import urllib.request
    try:
        req = urllib.request.Request(_safe_url(url), headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except Exception as e:  # noqa: BLE001
        print(f"添付DL失敗 {url}: {e}")
        return b""


def _store_attachments(row, attachments):
    """添付PDFをR2へ保存し、保存先情報を row['attachments'] に記録する。

    PDFマジックナンバー確認済みのものだけR2に保存。
    認証必須URL（GEPSなど）はHTMLが返るため自動的にスキップされ、source_urlのみ保持。
    R2未設定時は source_url のみ記録（UIでリンク表示）。
    PORTALは実仕様書がGEPS（認証必須）にあり取得できず、掴めるのは用語集等のみのため
    PDFダウンロード・R2保存は行わず、リンク(source_url)のみ記録する。
    """
    import re as _re
    is_portal = (row.get("source") == "PORTAL")
    stored = []
    for i, att in enumerate(attachments):
        r2_url = ""
        r2_key = ""
        if storage.r2_enabled() and not is_portal:
            data = _download(att["url"])
            if data and data.lstrip()[:4] == b"%PDF":
                safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", att["url"].split("/")[-1]) or f"file{i}.pdf"
                pub_date = (row.get("published_at") or "unknown").replace("/", "-")
                src_prefix = (row.get("source") or "misc").lower()
                key = f"{src_prefix}/{pub_date}_{row['id']}/{att['kind']}_{safe}"
                public = storage.upload_bytes(key, data, "application/pdf")
                r2_url = public if public.startswith("http") else ""
                r2_key = key
        stored.append({"name": att["name"], "kind": att["kind"],
                       "url": r2_url, "key": r2_key, "source_url": att["url"]})
    row["attachments"] = json.dumps(stored, ensure_ascii=False)
    row["attachments_checked"] = "1"

def _overview_from_r2(row: dict, max_pages: int = 8, max_lines: int = 50,
                      max_chars: int = 3000) -> str:
    """R2保存済みPDF（仕様書・公募要領等）からテキストを抽出する。
    max_* を大きくするとAI抽出の材料としてより多くの本文を返す。"""
    if not storage.r2_enabled():
        return ""
    try:
        atts = json.loads(row.get("attachments") or "[]")
    except Exception:
        return ""
    if not atts:
        return ""
    import io
    from pypdf import PdfReader
    s3 = storage.get_client()
    bucket = os.environ.get("R2_BUCKET", "")
    # 用語集・操作マニュアル等の汎用資料は案件と無関係なので除外する
    _skip = _re_summary.compile(r'用語集|ヘルプ|操作|マニュアル|手引|ガイド|よくある質問|ＦＡＱ|FAQ')
    # 公告文→公募要領→仕様書→調達資料の順に試行（意味ある文章が得られるまで）
    priority = ["公告文", "公募要領", "仕様書", "調達資料", "審査基準", "評価基準"]
    atts_sorted = sorted(atts, key=lambda a: next(
        (i for i, k in enumerate(priority) if k == a.get("kind")), 99))
    for att in atts_sorted:
        key = att.get("key", "")
        if not key:
            continue
        if _skip.search(str(att.get("name", ""))) or _skip.search(str(att.get("kind", ""))):
            continue  # 汎用資料（用語集等）はスキップ
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            data = obj["Body"].read()
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:max_pages])
            lines = [ln.strip() for ln in text.split("\n")
                     if ln.strip() and len(ln.strip()) > 8]
            if lines:
                return "\n".join(lines[:max_lines])[:max_chars]
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
    from pypdf import PdfReader
    s3 = storage.get_client()
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


def _interleave_by_priority(rows, priority_map):
    """優先度でグループ化し、各優先度内はソース間でラウンドロビンに並べる。
    件数の多いソース（愛知・岐阜等）が同一優先度内の他ソースの枠を独占し、
    件数の少ない新規ソースがいつまでも詳細取得の順番に回ってこない問題を防ぐ。"""
    from collections import defaultdict, deque
    by_priority = defaultdict(lambda: defaultdict(deque))
    for r in rows:
        by_priority[priority_map.get(r.get("source"), 1)][r.get("source")].append(r)
    ordered = []
    for pri in sorted(by_priority):
        buckets = list(by_priority[pri].values())
        while any(buckets):
            for b in buckets:
                if b:
                    ordered.append(b.popleft())
    return ordered


# 1回の実行で詳細/結果ページを取得する最大件数（負荷・実行時間対策。未取得分を順次埋める）
# 大阪の網羅性拡大(264→1979)や千葉・福井の建設工事追加で詳細取得バックログが
# 増えたため引き上げ。ソース横断のラウンドロビンで按分され1サイト当たりは緩やか。
MAX_DETAIL_PER_RUN = 400
MAX_AI_SUMMARY_PER_RUN = 300  # 1実行あたりAI要約（増分）の上限（コスト分散）
MAX_AI_REPAIR_PER_RUN = 30    # 1実行あたり英字混入要約の再生成上限
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


def retag_rows(rows):
    """全行のタグをタグマスター基準で再付与する。

    - タイトル＋AI要約＋本文(先頭3000字)から実務粒度タグを付与
    - タグが2件未満の案件は、タイトルが近い（bigram Jaccard≥0.45）
      タグ付き案件からタグを継承・統合する（情報の乏しい案件の取りこぼし対策）。
      過去案件・他ソースの類似案件も対象（同一ソースをやや優遇）。
    """
    import re as _re
    from scraper import generate_tags

    from tag_master import ORG_TAG_RULES
    _org_rules = [(_re.compile(p), t) for p, t in ORG_TAG_RULES]

    # 「過去の採択事例一覧」等は、当該公募と無関係な分野の事業者名・テーマが
    # 大量に列挙され、タグの誤爆・希釈を招くため、見出し以降を切り捨てる。
    _CASE_LIST_HEAD = _re.compile(
        r"(令和\d+年度採択(案件|事業)|採択(案件|事業|者)一覧|"
        r"これまでの採択|過去の採択|採択実績|採択事例)")

    def _for_tagging(detail):
        d = detail or ""
        m = _CASE_LIST_HEAD.search(d)
        return (d[:m.start()] if m else d)[:3000]

    sparse = []
    for r in rows:
        tags = generate_tags(r.get("title", ""), r.get("summary", ""),
                             _for_tagging(r.get("detail")))
        # 発注機関名から発注元ファセットタグを付与（売り先でアンテナを張る実務者向け）
        org = r.get("organization") or ""
        for pat, tag in _org_rules:
            if tag not in tags and pat.search(org):
                tags.append(tag)
        r["tags"] = ",".join(tags)
        if len(tags) < 3:
            sparse.append((r, len(tags)))

    def _bigrams(s):
        s = _re.sub(r"[\s　]", "", s or "")
        return {s[i:i + 2] for i in range(len(s) - 1)}

    # 継承元: タグが2件以上ついている案件
    donors = [(r2, _bigrams(r2.get("title", "")))
              for r2 in rows
              if len([t for t in (r2.get("tags") or "").split(",") if t]) >= 2]
    # タグが少ないほど積極的に継承する（0個=0.40 / 1個=0.45 / 2個=0.55）
    _THRESH = {0: 0.40, 1: 0.45, 2: 0.50}
    borrowed = 0
    for r, own_n in sparse:
        bg = _bigrams(r.get("title", ""))
        if not bg:
            continue
        best, score = None, 0.0
        for cand, cbg in donors:
            inter = len(bg & cbg)
            if not inter:
                continue
            j = inter / len(bg | cbg)
            if cand.get("source") == r.get("source"):
                j += 0.05  # 同一ソースの類似案件を優遇
            if j > score:
                best, score = cand, j
        if best is not None and score >= _THRESH.get(own_n, 0.55):
            own = [t for t in (r.get("tags") or "").split(",") if t]
            inherited = [t for t in (best.get("tags") or "").split(",") if t]
            merged = own + [t for t in inherited if t not in own]
            r["tags"] = ",".join(merged[:8])  # 継承しすぎ防止の上限
            borrowed += 1
    print(f"タグ再付与: 全{len(rows)}件 / タグ3件未満{len(sparse)}件中 "
          f"類似案件から継承{borrowed}件")


def main():
    from datetime import datetime, timezone
    os.makedirs(DATASET_DIR, exist_ok=True)
    # JST（UTC+9）基準に統一。GitHub Actions は UTC で動くため、date.today() だと
    # 深夜帯(JST)の実行が前日扱いになり、first_seen(新規取得日)が履歴表示(JST)と1日ずれる。
    _now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    today = _now_jst.strftime("%Y-%m-%d")
    # last_seen 用の日時（JST、分まで）
    now_jst = _now_jst.strftime("%Y-%m-%d %H:%M")

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

    # JOGMEC: 既存CSVの project_code（JOGMEC-XXXXX）から最大IDを算出（増分取得）
    jogmec_ids = []
    for r in existing.values():
        if r.get("source") == "JOGMEC" and (r.get("project_code") or "").startswith("JOGMEC-"):
            try:
                jogmec_ids.append(int(r["project_code"].split("-")[1]))
            except ValueError:
                pass
    jogmec_max_id = max(jogmec_ids) if jogmec_ids else 0
    print(f"JOGMEC最大取得済みID: {jogmec_max_id}")

    scraped = asyncio.run(run_all_scrapers(portal_date_from=portal_from, jogmec_max_id=jogmec_max_id))
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
                # スクレイパーが空値を返した場合に既存の正しい値を上書きしないよう or で補完する
                # （PORTAL等で一覧にタイトルが無い案件の title が空で上書きされる不具合の対策）
                "title": item.get("title") or prev.get("title", ""),
                "category": item.get("category") or prev.get("category", ""),
                "organization": item.get("organization") or prev.get("organization", ""),
                "prefecture": item.get("prefecture") or prev.get("prefecture", ""),
                "published_at": item.get("published_at") or prev.get("published_at", ""),
                "deadline": item.get("deadline") or prev.get("deadline", ""),
                "result_date": item.get("result_date") or prev.get("result_date", ""),
                "project_code": item.get("project_code") or prev.get("project_code", ""),
                "amount": item.get("amount") or prev.get("amount", ""),
                "result_url": item.get("result_url") or prev.get("result_url", ""),
                "source_category": item.get("source_category") or prev.get("source_category", ""),
                # summary はAI要約専用のため、スクレイパーの値で上書きしない
                "tags": item.get("tags") or prev.get("tags", ""),
                "source": item.get("source", prev.get("source", "")),
                "last_seen": now_jst,
            })
            update_count += 1
        else:
            max_id += 1
            row = {k: item.get(k, "") for k in FIELDNAMES}
            row["id"] = str(max_id)
            row["first_seen"] = today
            row["last_seen"] = now_jst
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
    _FETCH_SOURCES = {"NEDO", "JST", "PORTAL", "JOGMEC", "AICHI", "TOKYO", "OSAKA", "FUKUOKA",
                       "MIE", "GIFU", "YAMANASHI", "TOYAMA", "NAGANO", "SHIZUOKA", "FUKUI", "NIIGATA",
                       "TOCHIGI", "CHIBA", "KYOTO", "HYOGO", "SHIGA", "WAKAYAMA", "HIROSHIMA", "OKAYAMA", "EHIME", "KOCHI", "SAGA", "SHIMANE", "KUMAMOTO", "HOKKAIDO", "TOKUSHIMA", "NAGASAKI", "OKINAWA", "OITA", "AKITA", "FUKUSHIMA", "TOTTORI", "GUNMA"}

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
        # 電子調達システム(SuperCALS入札情報サービス)の案件はセッション依存で
        # スクレイパーが詳細確定済み。URLはポータル指定のため後段の詳細取得対象から
        # 除外する（千葉・福井CALS等。放置すると200件枠を空振りで食い潰す）。
        # ebidPPIPublish はSuperCALS PPIの共通パス（chiba-ep-bis/ebid.pref.fukui等）。
        if "ebidPPIPublish" in (r.get("url") or "") or "efftis.jp" in (r.get("url") or ""):
            return False
        # budget_checked=1 が「詳細取得を一度試みた」フラグ。立っていなければ必ず取得する
        if (r.get("budget_checked") or "") != "1":
            return True
        return False

    targets = [r for r in merged.values() if needs_fetch(r)]
    # 取得の優先順位：件数の少ない重要ソース（県/都公募など）を先に、大量のPORTALは後回し。
    # PORTAL(数千件)が1回200件の枠を食い尽くし、県公募の本文・要約が埋まらない問題への対策。
    _FETCH_PRIORITY = {"AICHI": 0, "TOKYO": 0, "OSAKA": 0, "FUKUOKA": 0, "MIE": 0, "GIFU": 0,
                       "YAMANASHI": 0, "TOYAMA": 0, "NAGANO": 0, "SHIZUOKA": 0, "FUKUI": 0,
                       "NIIGATA": 0, "TOCHIGI": 0, "CHIBA": 0, "KYOTO": 0, "HYOGO": 0, "SHIGA": 0, "WAKAYAMA": 0, "HIROSHIMA": 0, "OKAYAMA": 0, "EHIME": 0, "KOCHI": 0, "SAGA": 0, "SHIMANE": 0, "KUMAMOTO": 0, "HOKKAIDO": 0, "TOKUSHIMA": 0, "NAGASAKI": 0, "OKINAWA": 0, "OITA": 0, "AKITA": 0, "FUKUSHIMA": 0, "TOTTORI": 0, "GUNMA": 0,
                       "NEDO": 1, "JST": 1, "JOGMEC": 1, "PORTAL": 2}
    targets = _interleave_by_priority(targets, _FETCH_PRIORITY)
    print(f"概要/予算を取得（増分）: {min(len(targets), MAX_DETAIL_PER_RUN)}件")
    for r in targets[:MAX_DETAIL_PER_RUN]:
        src = r.get("source", "")
        if src == "JST":
            info = fetch_jst_detail(r["url"])
        elif src == "PORTAL":
            info = fetch_portal_detail(r["url"])
        elif src == "JOGMEC":
            info = fetch_jogmec_detail(r["url"])
        elif src == "AICHI":
            info = fetch_aichi_detail(r["url"])
        elif src == "TOKYO":
            info = fetch_tokyo_detail(r["url"])
        elif src == "OSAKA":
            # 入札(EbController)とプロポーザル(公式サイト記事)でURL形式が異なる
            info = (fetch_osaka_detail(r["url"]) if "EbController" in r["url"]
                    else fetch_osaka_proposal_detail(r["url"]))
        elif src == "FUKUOKA":
            info = fetch_fukuoka_detail(r["url"])
        elif src == "MIE":
            info = fetch_mie_detail(r["url"])
        elif src == "GIFU":
            info = fetch_gifu_detail(r["url"])
        elif src == "YAMANASHI":
            info = fetch_yamanashi_detail(r["url"])
        elif src == "TOYAMA":
            info = fetch_toyama_detail(r["url"])
        elif src == "NAGANO":
            info = fetch_nagano_detail(r["url"])
        elif src == "SHIZUOKA":
            info = fetch_shizuoka_detail(r["url"])
        elif src == "FUKUI":
            info = fetch_fukui_detail(r["url"])
        elif src == "NIIGATA":
            info = fetch_niigata_detail(r["url"])
        elif src == "TOCHIGI":
            info = fetch_tochigi_detail(r["url"])
        elif src == "CHIBA":
            info = fetch_chiba_detail(r["url"])
        elif src == "KYOTO":
            info = fetch_kyoto_detail(r["url"])
        elif src == "HYOGO":
            info = fetch_hyogo_detail(r["url"])
        elif src == "SHIGA":
            info = fetch_shiga_detail(r["url"])
        elif src == "WAKAYAMA":
            info = fetch_wakayama_detail(r["url"])
        elif src == "HIROSHIMA":
            info = fetch_hiroshima_detail(r["url"])
        elif src == "OKAYAMA":
            info = fetch_okayama_detail(r["url"])
        elif src == "EHIME":
            info = fetch_ehime_detail(r["url"])
        elif src == "KOCHI":
            info = fetch_kochi_detail(r["url"])
        elif src == "SAGA":
            info = fetch_saga_detail(r["url"])
        elif src == "SHIMANE":
            info = fetch_shimane_detail(r["url"])
        elif src == "KUMAMOTO":
            info = fetch_kumamoto_detail(r["url"])
        elif src == "HOKKAIDO":
            info = fetch_hokkaido_detail(r["url"])
        elif src == "TOKUSHIMA":
            info = fetch_tokushima_detail(r["url"])
        elif src == "NAGASAKI":
            info = fetch_nagasaki_detail(r["url"])
        elif src == "OKINAWA":
            info = fetch_okinawa_detail(r["url"])
        elif src == "OITA":
            info = fetch_oita_detail(r["url"])
        elif src == "AKITA":
            info = fetch_akita_detail(r["url"])
        elif src == "FUKUSHIMA":
            info = fetch_fukushima_detail(r["url"])
        elif src == "TOTTORI":
            info = fetch_tottori_detail(r["url"])
        elif src == "GUNMA":
            info = fetch_gunma_detail(r["url"])
        else:
            info = fetch_nedo_detail(r["url"])  # 概要＋予算（本文→無ければPDF）＋予定
        if info:  # ページ取得成功
            new_detail = info.get("detail", "")
            cur_detail = (r.get("detail") or "").strip()
            # PORTAL はゴミdetailをリセット済みなので常に上書き。他ソースは空のときのみ
            if new_detail and (not cur_detail or r.get("source") == "PORTAL"):
                r["detail"] = _strip_detail_boilerplate(new_detail)  # 定型文除去して保持
            # タイトルが一覧から取れなかった案件（PORTALで稀に発生）は
            # 詳細ページの「調達案件名称」で補完する
            if not (r.get("title") or "").strip() and info.get("title"):
                r["title"] = info["title"]
            # 掲載日が一覧から取れないソース（愛知プロポ等）は詳細ページの値で補完
            if info.get("published_at") and not (r.get("published_at") or "").strip():
                r["published_at"] = info["published_at"]
            # 添付PDF（仕様書・公募要領）を先にR2へ保存 → AI抽出の材料に使う
            if (r.get("attachments_checked") or "") != "1":
                _store_attachments(r, info.get("attachments", []))
            # AI抽出：公告本文＋添付PDF抜粋（仕様書等）を材料にsummary/deadline/amount/scheduleへ
            ai_extracted = {}
            need_summary = not (r.get("summary") or "").strip()
            if need_summary:
                detail_for_ai = (r.get("detail") or new_detail or "").strip()
                # 要約対象のときだけPDF本文を取得（無駄なR2アクセスを避ける）。
                # PORTALは使える仕様書が取れないためPDF抜粋は取得しない。
                pdf_text = ""
                if src != "PORTAL":
                    try:
                        pdf_text = _overview_from_r2(r, max_pages=12, max_lines=120, max_chars=5000)
                    except Exception as e:  # noqa: BLE001
                        print(f"PDF抜粋取得失敗: {e}")
                ai_input = detail_for_ai[:4000]
                if pdf_text:
                    ai_input += "\n\n【添付資料抜粋】\n" + pdf_text
                if len(ai_input.strip()) > 100:
                    ai_extracted = _ai_extract(ai_input, r.get("title", ""))
                    if ai_extracted.get("bullets"):
                        r["summary"] = _bullets_to_summary(ai_extracted["bullets"])
                    if ai_extracted.get("deadline"):
                        r["deadline"] = ai_extracted["deadline"]
                    if ai_extracted.get("amount") and not (r.get("amount") or "").strip():
                        r["amount"] = ai_extracted["amount"]
                    if ai_extracted.get("schedule"):
                        r["schedule"] = json.dumps(ai_extracted["schedule"], ensure_ascii=False)
            # AIが拾えなかった項目はスクレイパー値で補完
            if not ai_extracted.get("amount") and info.get("budget") and not (r.get("amount") or "").strip():
                r["amount"] = info["budget"]
            if not ai_extracted.get("schedule") and info.get("schedule") and not (r.get("schedule") or "").strip():
                r["schedule"] = json.dumps(info["schedule"], ensure_ascii=False)
            # PORTAL: 調達種別→category、公開終了日→close_date を保存
            if src == "PORTAL":
                if info.get("category"):
                    r["category"] = info["category"]
                # close_date（公開終了日）は常にスクレイパー値を使用
                if info.get("close_date"):
                    r["close_date"] = info["close_date"]
                # deadline（入札書提出期限）はAI抽出を優先、なければスクレイパー値
                if not ai_extracted.get("deadline") and info.get("deadline") and not (r.get("deadline") or "").strip():
                    r["deadline"] = info["deadline"]
            r["budget_checked"] = "1"  # 予算確認済み（空でも再取得しない）
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
                row["awardee"] = _ai_split_awardee(info["awardee"])
                row["awardee_checked"] = "1"  # 取得できた時だけ監視終了
            if info.get("result_date") and not (row.get("result_date") or "").strip():
                row["result_date"] = info["result_date"]
            aw_count += 1
            time.sleep(DETAIL_SLEEP)
    print(f"決定事業者を確認（増分）: {aw_count}件")

    # 既存の事業者名が未分割（｜なし・長い）のものをAIで分割
    split_count = 0
    for r in merged.values():
        aw = (r.get("awardee") or "").strip()
        if not aw or "｜" in aw or len(aw) < 20:
            continue
        new_aw = _ai_split_awardee(aw)
        if new_aw != aw:
            r["awardee"] = new_aw
            split_count += 1
    if split_count:
        print(f"事業者名をAI分割（バックフィル）: {split_count}件")

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

    # R2保存済みPDFから概要を補完
    # - detail未記入の案件（全ソース）
    # - JOGMECでdetailが薄い案件（HTMLはナビ文字列程度しか取れないため400文字未満はPDFで上書き）
    r2_detail_count = 0
    for r in merged.values():
        if not (r.get("attachments") or "").strip():
            continue  # R2にPDFなし
        det = (r.get("detail") or "").strip()
        is_thin_jogmec = r.get("source") == "JOGMEC" and len(det) < 400
        if det and not is_thin_jogmec:
            continue  # 十分なdetailあり
        overview = _overview_from_r2(r)
        if overview:
            r["detail"] = overview
            # 薄いdetailをPDFで上書きした場合はsummaryもリセットして再要約させる
            if is_thin_jogmec and det:
                r["summary"] = ""
            r2_detail_count += 1
    print(f"R2 PDFから概要補完: {r2_detail_count}件")

    # 【増分】detailはあるがsummaryが空の案件をAI抽出する。
    # needs_fetch()を通らない既存案件（budget_checked=1済み）もここでカバーする。
    summarized_count = 0
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY"):
        for r in merged.values():
            if summarized_count >= MAX_AI_SUMMARY_PER_RUN:
                break
            if (r.get("summary") or "").strip():
                continue
            det = (r.get("detail") or "").strip()
            if len(det) < 100:
                continue
            extracted = _ai_extract(det, r.get("title", ""))
            if extracted.get("bullets"):
                r["summary"] = _bullets_to_summary(extracted["bullets"])
                summarized_count += 1
            if extracted.get("deadline"):
                r["deadline"] = extracted["deadline"]
            if extracted.get("amount") and not (r.get("amount") or "").strip():
                r["amount"] = extracted["amount"]
            if extracted.get("schedule") and not (r.get("schedule") or "").strip():
                r["schedule"] = json.dumps(extracted["schedule"], ensure_ascii=False)
        if summarized_count:
            print(f"AI抽出（増分）: {summarized_count}件")

    # 【日次セーフティ】英字混入の誤生成が残る古形式summaryを再生成して修復。
    repaired = 0
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY"):
        for r in merged.values():
            if repaired >= MAX_AI_REPAIR_PER_RUN:
                break
            summ = (r.get("summary") or "").strip()
            if not summ or not _has_corrupt_latin(summ):
                continue
            det = (r.get("detail") or "").strip()
            if len(det) < 100:
                continue
            extracted = _ai_extract(det, r.get("title", ""))
            new_summ = _bullets_to_summary(extracted.get("bullets") or [])
            if new_summ and not _has_corrupt_latin(new_summ):
                r["summary"] = new_summ
                if extracted.get("deadline"):
                    r["deadline"] = extracted["deadline"]
                repaired += 1
        if repaired:
            print(f"英字混入の要約を再生成（修復）: {repaired}件")

    # タグ再付与（毎回全件）：タグマスターの改良が過去案件にも自動反映されるようにする。
    # 情報が乏しくタグ0件の案件は、タイトルが最も近い過去案件からタグを継承する。
    try:
        retag_rows(list(merged.values()))
    except Exception as e:  # noqa: BLE001
        print(f"タグ再付与失敗: {e}")

    # 書き出し前に、ID未設定の行へ必ずIDを採番する（空IDはDB投入をUNIQUE制約で壊すため）
    _id_max = max((int(r["id"]) for r in merged.values()
                   if (r.get("id") or "").strip().isdigit()), default=0)
    _no_id = 0
    for r in merged.values():
        if not (r.get("id") or "").strip().isdigit():
            _id_max += 1
            r["id"] = str(_id_max)
            _no_id += 1
    if _no_id:
        print(f"ID未設定の行にID採番: {_no_id}件")

    # ID順に並べて書き出し
    rows = sorted(merged.values(), key=lambda r: int(r.get("id") or 0))
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in FIELDNAMES})

    print(f"書き出し完了: {CSV_PATH} ({len(rows)}件)")

    # 実行履歴ログ（開発ページ用）。直近50件をローリング保存する。
    try:
        _write_update_log(
            scraped=len(scraped),
            new=new_count,
            updated=update_count,
            total=len(rows),
            portal_retry=portal_retry,
            repaired=repaired,
        )
    except Exception as e:  # noqa: BLE001
        print(f"実行ログ書き出し失敗: {e}")


def _write_update_log(scraped, new, updated, total, portal_retry, repaired):
    """実行ごとの統計を dataset/update_log.json に追記する（直近50件）。"""
    from datetime import datetime, timezone
    log_path = os.path.join(DATASET_DIR, "update_log.json")
    in_tok = _AI_USAGE["input_tokens"]
    out_tok = _AI_USAGE["output_tokens"]
    cost = in_tok / 1_000_000 * _HAIKU_IN_PER_M + out_tok / 1_000_000 * _HAIKU_OUT_PER_M
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scraped": scraped,
        "new": new,
        "updated": updated,
        "total": total,
        "portal_retry": portal_retry,
        "repaired": repaired,
        "ai_calls": _AI_USAGE["calls"],
        "ai_input_tokens": in_tok,
        "ai_output_tokens": out_tok,
        "ai_cost_usd": round(cost, 4),
    }
    history = []
    alltime_cost = 0.0
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                history = data.get("runs", []) if isinstance(data, dict) else []
                # 直近50件の合計しか保持していなかった旧データからの移行時は、
                # 既存のrunsの合計を初期値として引き継ぐ（過去分を消さない）
                alltime_cost = float(data.get("cumulative_cost_usd_alltime")
                                     if isinstance(data, dict) and data.get("cumulative_cost_usd_alltime") is not None
                                     else sum(float(r.get("ai_cost_usd") or 0) for r in history))
        except (ValueError, OSError):
            history = []
    history.append(entry)
    history = history[-50:]  # 表示用の直近50件のみ保持（生涯累計とは別管理）
    # 直近50件の合計（＝古い実行ほど表示から消えていく参考値）
    total_cost_recent = round(sum(float(r.get("ai_cost_usd") or 0) for r in history), 4)
    # 生涯累計（このカウンタ自体は50件枠の対象外・一切減らない）
    alltime_cost = round(alltime_cost + entry["ai_cost_usd"], 4)
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump({"runs": history,
                   "cumulative_cost_usd_recent": total_cost_recent,
                   "cumulative_cost_usd_alltime": alltime_cost}, f,
                  ensure_ascii=False, indent=2)
    print(f"実行ログ更新: AI {entry['ai_calls']}回 / 推定コスト ${entry['ai_cost_usd']}")


if __name__ == "__main__":
    main()
