# -*- coding: utf-8 -*-
"""データCSVをR2(非公開バケット)と同期する。gitからデータを切り離すための土台。

  python scripts/sync_r2.py pull   # R2 → ローカル dataset/tenders.csv（スクレイプ前の土台取得）
  python scripts/sync_r2.py push   # ローカル dataset/tenders.csv → R2（件数チェック通過後に保存）

安全設計:
- pull: R2が有効(R2_DATA_BUCKET設定済)なのに取得失敗/空なら exit 1 で中断する。
  （古い/空のデータを土台にスクレイプ→R2上書き、という取りこぼし事故を防ぐ）
  R2未設定(ローカル等)なら no-op（gitのCSVをそのまま使う）。
- push: R2未設定なら no-op。
"""
import sys, io, os, csv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, errors="replace", encoding="utf-8")
csv.field_size_limit(10 ** 7)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV = os.path.join(ROOT, "dataset", "tenders.csv")

# ローカル実行時は .env を読み込む（CIはSecretsから環境変数が渡る）
envp = os.path.join(ROOT, ".env")
if os.path.exists(envp):
    for line in open(envp, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, os.path.join(ROOT, "backend"))
import gzip, tempfile  # noqa: E402
import storage  # noqa: E402
from csv_sqlite import csv_to_db, db_to_csv  # noqa: E402

MIN_BYTES = 30000    # gz済DBがこれ未満は「壊れ/空」とみなす
DB_KEY = "tenders.db.gz"
LOCAL_DB_GZ = os.path.join(ROOT, "dataset", "tenders.db.gz")


def _tmp(name):
    return os.path.join(tempfile.gettempdir(), name)


def pull():
    """R2(またはgit同梱)のSQLite(gz)を取得し、パイプライン用にCSVへ展開する。"""
    if not storage.data_bucket_enabled():
        # ローカル: CSVが無くて db.gz があれば展開（開発時の利便）
        if not os.path.exists(CSV) and os.path.exists(LOCAL_DB_GZ):
            db = _tmp("pull_local.db")
            open(db, "wb").write(gzip.decompress(open(LOCAL_DB_GZ, "rb").read()))
            n = db_to_csv(db, CSV)
            print(f"[pull] R2未設定 → git同梱DBからCSV展開 {n}行")
        else:
            print("[pull] R2未設定 → 既存CSVをそのまま使用（no-op）")
        return 0
    data = storage.download_data(DB_KEY)
    if data and len(data) >= MIN_BYTES:
        db = _tmp("pull.db")
        open(db, "wb").write(gzip.decompress(data))
        n = db_to_csv(db, CSV)
        print(f"[pull] R2から取得: {len(data):,} bytes(gz) → DB → CSV {n}行")
        return 0
    # 移行期: R2にdb.gzがまだ無ければ、旧 tenders.csv を土台にする（次のpushでdb.gz化）
    old = storage.download_data("tenders.csv")
    if old and len(old) >= 100000:
        open(CSV, "wb").write(old)
        print(f"[pull] R2にdb.gz無し → 旧tenders.csvを土台に {len(old):,}B（次のpushでSQLite化）")
        return 0
    print("[pull] ⚠ R2からの取得に失敗/空。古いデータでの上書きを防ぐため中断します。")
    return 1


def push():
    """ローカルCSVをSQLite化・gzipしてR2へ保存する（R2の正はSQLite）。"""
    if not os.path.exists(CSV):
        print("[push] ⚠ ローカルCSVが無い。pushスキップ。")
        return 1
    db = _tmp("push.db")
    n = csv_to_db(CSV, db)
    gz = gzip.compress(open(db, "rb").read(), 6)
    if len(gz) < MIN_BYTES:
        print("[push] ⚠ 生成DBが小さすぎ。安全のためpushしません。")
        return 1
    if not storage.data_bucket_enabled():
        print(f"[push] R2未設定 → pushスキップ（DB化のみ確認: {n}行 / {len(gz):,}B）")
        return 0
    if storage.upload_data(DB_KEY, gz, "application/gzip"):
        print(f"[push] R2へ保存: SQLite {n}行 / {len(gz):,} bytes(gz)")
        return 0
    print("[push] ⚠ R2へのアップロード失敗。")
    return 1


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "pull":
        sys.exit(pull())
    elif mode == "push":
        sys.exit(push())
    else:
        print("usage: python scripts/sync_r2.py [pull|push]")
        sys.exit(2)
