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
import storage  # noqa: E402

MIN_BYTES = 100000  # これ未満は「壊れ/空」とみなす（正常時は数十MB）


def pull():
    if not storage.data_bucket_enabled():
        print("[pull] R2未設定 → gitのCSVをそのまま使用（no-op）")
        return 0
    data = storage.download_data("tenders.csv")
    if data and len(data) >= MIN_BYTES:
        with open(CSV, "wb") as f:
            f.write(data)
        print(f"[pull] R2から取得: {len(data):,} bytes → {CSV}")
        return 0
    # R2は有効なのに取れない/空 → 古い土台での上書き事故を避けるため中断
    print("[pull] ⚠ R2からの取得に失敗/空。古いデータでの上書きを防ぐため中断します。")
    return 1


def push():
    if not storage.data_bucket_enabled():
        print("[push] R2未設定 → pushスキップ（no-op）")
        return 0
    if not os.path.exists(CSV):
        print("[push] ⚠ ローカルCSVが無い。pushスキップ。")
        return 1
    data = open(CSV, "rb").read()
    if len(data) < MIN_BYTES:
        print("[push] ⚠ ローカルCSVが小さすぎ。安全のためpushしません。")
        return 1
    if storage.upload_data("tenders.csv", data, "text/csv; charset=utf-8"):
        print(f"[push] R2へ保存: {len(data):,} bytes")
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
