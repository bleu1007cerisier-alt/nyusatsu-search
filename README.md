# 入札・プロポーザル検索

日本全国の官公庁・公的機関の入札・公募（プロポーザル）情報を一元検索できるWebアプリです。

🌐 公開サイト: https://nyusatsu-search.onrender.com/

## 特徴
- キーワード検索 ／ 種別（入札・プロポーザル）・都道府県・タグでの絞り込み
- **状態の3段階表示**：募集中 / 受付終了 / 事業者決定
- **プロジェクト追跡**：同じ事業名の公募回を詳細ページで時系列表示
- 公示日・締切日・予算規模・決定事業者（取得できる場合）を表示
- スクレイピングと表示を分離した軽量構成

## アーキテクチャ
```
GitHub Actions（毎日 9:30/13:30/15:30 JST）
  → scripts/build_dataset.py がスクレイピングし dataset/tenders.csv に蓄積
  → 変更を自動コミット → Render が自動デプロイ
Webサイトは起動時に dataset/tenders.csv を読み込むだけ（実行時スクレイピングなし）
```

## 構成
- バックエンド: Python + FastAPI（`backend/`）
- データ: `dataset/tenders.csv`（正本）/ SQLite（起動時にCSVから生成）
- フロント: HTML + Tailwind（`frontend/`）
- スクレイピング: aiohttp + BeautifulSoup（`backend/scraper.py`、`scripts/build_dataset.py`）

## ローカルでの起動
```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8130
# → http://127.0.0.1:8130/
```
データを手動で更新する場合: `python scripts/build_dataset.py`

## デプロイ（Render.com）
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`（`$PORT` 必須）

## 引き継ぎ・詳細
開発を引き継ぐ場合は [ONBOARDING.md](ONBOARDING.md) を参照してください。
現状・データ仕様・残課題をまとめています。
