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


def r2_enabled() -> bool:
    return all(
        os.environ.get(k)
        for k in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    )


def _client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


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
