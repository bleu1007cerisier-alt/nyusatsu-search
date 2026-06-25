# 入札・プロポーザル検索

日本全国の官公庁・自治体の入札案件・プロポーザル案件を一元検索できるWebアプリです。

## 起動方法

```bash
pip install -r requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

ブラウザで http://localhost:8000 を開いてください。

## 機能

- キーワード検索
- 種別フィルター（入札 / プロポーザル）
- 都道府県フィルター
- 締切が近い順表示
- 最新データ自動取得
