# -*- coding: utf-8 -*-
"""AI要約バックログ（本文あり・要約空）を一括生成する単発スクリプト。
.env の ANTHROPIC_API_KEY を読み込み、build_dataset の AI抽出＋要約正規化を再利用する。
使い方: python scripts/backfill_ai_summaries.py [--limit N] [--write]
        --write 無しは対象件数の確認のみ。--limit で件数制限（疎通確認用）。
"""
import sys, io, os, csv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, errors="replace", encoding="utf-8")
csv.field_size_limit(10 ** 7)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# .env を読み込んで環境変数へ（build_dataset は load_dotenv しないため）
envp = os.path.join(ROOT, ".env")
if os.path.exists(envp):
    for line in open(envp, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from build_dataset import _ai_extract, _bullets_to_summary, normalize_summary  # noqa: E402

CSV = os.path.join(ROOT, "dataset", "tenders.csv")
WRITE = "--write" in sys.argv
LIMIT = None
if "--limit" in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index("--limit") + 1])

has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY"))
print("APIキー:", "あり" if has_key else "なし")

with open(CSV, encoding="utf-8-sig") as f:
    rd = csv.DictReader(f)
    rows = list(rd)
    cols = rd.fieldnames

backlog = [r for r in rows if len((r.get("detail") or "").strip()) >= 100
           and not (r.get("summary") or "").strip()]
print("バックログ:", len(backlog), "件", ("/ 上限%d" % LIMIT if LIMIT else ""))
if not WRITE:
    print("[DRY] --write で生成。--limit N で件数制限。")
    sys.exit(0)
if not has_key:
    print("APIキーが無いため中止")
    sys.exit(1)

done = 0
for r in (backlog[:LIMIT] if LIMIT else backlog):
    det = (r.get("detail") or "").strip()
    try:
        ex = _ai_extract(det, r.get("title", ""))
    except Exception as e:  # noqa: BLE001
        print("  抽出失敗:", r.get("title", "")[:24], str(e)[:60])
        continue
    bullets = ex.get("bullets") or []
    if bullets:
        r["summary"] = normalize_summary(_bullets_to_summary(bullets))
        if ex.get("deadline") and not (r.get("deadline") or "").strip():
            r["deadline"] = ex["deadline"]
        if ex.get("amount") and not (r.get("amount") or "").strip():
            r["amount"] = ex["amount"]
        done += 1
        if done % 25 == 0:
            print("  ...%d件生成" % done)

with open(CSV, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in cols})
print("[WRITE] AI要約生成:", done, "件")
