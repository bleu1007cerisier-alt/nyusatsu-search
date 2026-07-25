"""
Cloudflare R2（S3互換）への添付ファイル保存。

環境変数が未設定の場合は何もしない（ローカルや鍵未登録時は安全にスキップ）。
GitHub Actions では Secrets から以下を渡す：
  R2_ENDPOINT          例: https://<accountid>.r2.cloudflarestorage.com
  R2_ACCESS_KEY_ID
  R2_SECRET_ACCESS_KEY
  R2_BUCKET            例: nyusatsu-docs
  R2_PUBLIC_URL        例: https://pub-xxxx.r2.dev  または独自ドメイン（公開する場合）
"""

import os

_cached_client = None


def r2_enabled() -> bool:
    return all(
        os.environ.get(k)
        for k in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    )


def _client():
    """R2クライアントをプロセス内で使い回す（毎回新規作成すると接続の使い捨てが
    連続し、Cloudflare側やネットワーク経路で接続リセットが起きやすくなるため）。
    タイムアウト・自動リトライも明示設定し、一時的な切断を自己回復させる。
    """
    global _cached_client
    if _cached_client is None:
        import boto3
        from botocore.config import Config
        _cached_client = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
            config=Config(
                connect_timeout=10, read_timeout=30,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
    return _cached_client


def get_client():
    """使い回し・リトライ設定済みのR2クライアントを返す（他モジュールから直接読む場合用）。"""
    return _client()


def object_exists(key: str) -> bool:
    if not r2_enabled():
        return False
    try:
        _client().head_object(Bucket=os.environ["R2_BUCKET"], Key=key)
        return True
    except Exception:
        return False


def upload_bytes(key: str, data: bytes, content_type: str = "application/pdf") -> str:
    """データをR2へ保存し、公開URL（R2_PUBLIC_URL設定時）またはキーを返す。未設定なら空文字。"""
    if not r2_enabled():
        return ""
    try:
        _client().put_object(
            Bucket=os.environ["R2_BUCKET"], Key=key, Body=data, ContentType=content_type
        )
    except Exception as e:  # noqa: BLE001
        print(f"R2アップロード失敗 {key}: {e}")
        return ""
    base = (os.environ.get("R2_PUBLIC_URL") or "").rstrip("/")
    return f"{base}/{key}" if base else key


def download_bytes(key: str) -> bytes:
    """R2からオブジェクトを取得してbytesで返す。未設定・不在・失敗時は None。"""
    if not r2_enabled():
        return None
    try:
        obj = _client().get_object(Bucket=os.environ["R2_BUCKET"], Key=key)
        return obj["Body"].read()
    except Exception as e:  # noqa: BLE001
        print(f"R2ダウンロード失敗 {key}: {e}")
        return None


# ── データセット（tenders.csv）専用の非公開バケット ─────────────────────────
# 公開バケット(R2_BUCKET=nyusatsu-docs, PDF配信用)にデータCSVを置くと、公開URLで
# 誰でも全データをDLでき有料モデルを崩す。データは必ず別の「非公開」バケット
# R2_DATA_BUCKET に置く。R2_DATA_BUCKET 未設定時はこの経路を無効化し、gitのCSVを使う。
def data_bucket_enabled() -> bool:
    return bool(
        os.environ.get("R2_DATA_BUCKET")
        and os.environ.get("R2_ENDPOINT")
        and os.environ.get("R2_ACCESS_KEY_ID")
        and os.environ.get("R2_SECRET_ACCESS_KEY")
    )


def upload_data(key: str, data: bytes, content_type: str = "application/octet-stream") -> bool:
    """非公開データバケット(R2_DATA_BUCKET)へ保存。未設定なら何もせず False。"""
    if not data_bucket_enabled():
        return False
    try:
        _client().put_object(
            Bucket=os.environ["R2_DATA_BUCKET"], Key=key, Body=data, ContentType=content_type
        )
        return True
    except Exception as e:  # noqa: BLE001
        print(f"R2データ保存失敗 {key}: {e}")
        return False


def download_data(key: str) -> bytes:
    """非公開データバケット(R2_DATA_BUCKET)から取得。未設定・不在・失敗時は None。"""
    if not data_bucket_enabled():
        return None
    try:
        obj = _client().get_object(Bucket=os.environ["R2_DATA_BUCKET"], Key=key)
        return obj["Body"].read()
    except Exception as e:  # noqa: BLE001
        print(f"R2データ取得失敗 {key}: {e}")
        return None
