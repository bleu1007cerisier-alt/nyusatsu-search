# 入札・プロポーザル検索アプリ 引き継ぎガイド

別のClaude Code環境／アカウントで、このプロジェクトの続きを行うための引き継ぎ資料です。
まずこのファイルを最初から最後まで読み、`backend/` と `dataset/tenders.csv` を確認してください。

## 0. このプロジェクトは何か
- 日本全国の入札・公募（プロポーザル）情報を一元検索できるWebアプリ。
- オーナーの目標：**月5万円の副収入**（有料プラン980円/月 × 50人を想定）。
- 公開中: **https://nyusatsu-search.onrender.com/**
- GitHub: **https://github.com/bleu1007cerisier-alt/nyusatsu-search**

## 1. 技術構成
- バックエンド: Python + FastAPI（`backend/main.py`）
- DB: SQLite（`backend/database.py`）。ただし**永続的な真のデータは `dataset/tenders.csv`**（後述）。
- フロント: 素のHTML + Tailwind CDN（`frontend/index.html` 一覧、`frontend/detail.html` 詳細ページ）
- スクレイピング: aiohttp + BeautifulSoup（`backend/scraper.py`）、PDFは未使用
- デプロイ: Render.com（無料プラン）。`render.yaml` あり。起動コマンドは必ず `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`（**$PORT必須**）。
- 自動更新: GitHub Actions（`.github/workflows/scrape.yml`）

## 2. 最重要：データの流れ（集める=cron / 見せる=軽量サイト）
```
GitHub Actions（毎日 9:30/13:30/15:30 JST, cron）
  → python scripts/build_dataset.py を実行（★スクレイピングはここだけ）
  → NEDOを巡回し dataset/tenders.csv に追記マージ（消さず蓄積）。**増分更新**：概要・予算・決定事業者は新規/未取得の案件だけ取得し、既存は再ダウンロードしない
  → 変更があれば自動コミット&push
  → Render が自動再デプロイ
  → サイトは起動時に dataset/tenders.csv をDBへ読込（load_dataset_into_db）。★実行時スクレイピングはしない
```
- だからサイトは軽い。データの正本は `dataset/tenders.csv`（utf-8-sig, id列で安定）。
- ローカルで手動更新するなら: `python scripts/build_dataset.py`

## 3. データ項目と仕様
- 種別(category): 入札 / プロポーザル（現状ほぼ全てプロポーザル）
- 状態(status): **DB非保存・配信時に算出**。`result_date`あり→事業者決定 / 締切<今日→受付終了 / それ以外→募集中。
- `project_code`(事業コード 【Pxxxxx】): NEDOの予算番号。**別テーマの公募も同番号にぶら下がる**ため、関連案件のまとめには使わない。
- 関連案件（プロジェクト経過）: **事業名(title完全一致)**で束ねる。詳細ページ「同じ事業名の公募回」。
- タグ: `scraper.py` の `TAG_KEYWORDS`（洋上風力/水素/脱炭素・GX/AI・データ 等）＋NEDO分野名。
- 予算規模(amount): 公募詳細ページ本文の「予算規模：」を優先抽出し**万円表記に統一**（`_format_amount`）。本文に無ければ**公募要領PDF**から抽出（`_pdf_budget_from_soup`/`_extract_pdf_budget`、pypdf使用。「1件あたり」優先→「全体予算」）。表記例: `15,000万円未満（税込）` `1件あたり3,000万円以内` `全体予算100,000万円程度`。
- 決定事業者(awardee): 結果ページの「実施予定先」直後の社名を抽出（HTMLに社名がある案件のみ。添付PDFのみの案件は取得不可）。`awardee_checked=1`で確認済みを記録し再取得しない。
- 概要(detail)は**連絡先・メール・電話を除外**（`_CONTACT`）。
- 予定(schedule): 説明会・申込期限・応募締切・事前相談等を時系列抽出（`_extract_schedule`）しJSON文字列で`schedule`列に保存。詳細ページ「🗓️予定」に表示（過去はグレー）。
- 概要は業務内容段落(_is_scope)を先頭に並べ替え。類似案件: タグ一致数で算出し詳細最下段表示(/api/tenders/{id} の similar)。各ページに自動収集の注意書き。
- 添付ファイル(attachments): 公募要領/仕様書/評価基準PDFを `backend/storage.py`(Cloudflare R2, S3互換)へ保存。**R2_* 環境変数(GitHub Secrets)が未設定なら保存はスキップ**(原文リンクのみ)。build_dataset が新規案件のみDL→R2保存しattachments(JSON)に記録。CSV列: attachments/attachments_checked、DB attachments列。要 boto3。
- 予算抽出はHTML本文→無ければ公募要領PDF。「【予算規模】」等の記号区切りにも対応。budget_checkedで未取得分の一度きり再取得。カバレッジ約53/100件。
- 文字コード: NEDOはページによりUTF-8/Shift_JIS混在。`_decode`で自動判定。`_normalize_date`は空白除去してから日付判定（重要）。
- NEDO分野ページの表は5列 `[事業名 | 予告掲載日 | 公募開始日(詳細リンク) | 公募締切日 | 結果(結果リンク)]`。

## 4. API（`backend/main.py`）
- `GET /api/tenders` … 検索。params: q, category, prefecture, source, tag, status, skip, limit。並び順=募集中(締切近い順)→受付終了→事業者決定。
- `GET /api/tenders/{id}` … 詳細。related[]（同名の公募回）を含む。
- `GET /api/stats` … 件数・状態別件数・タグ集計。
- `GET /tender/{id}` … 詳細ページHTML（detail.html）を返すルート。
- レスポンスは no-cache ヘッダ付き（古い画面のキャッシュ防止）。

## 5. ローカル開発手順
```
git clone https://github.com/bleu1007cerisier-alt/nyusatsu-search.git
cd nyusatsu-search
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
.venv/Scripts/python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8130
# → http://127.0.0.1:8130/  （dataset/tenders.csv を読み込んで表示）
```
- データを作り直す: `.venv/Scripts/python.exe scripts/build_dataset.py`（NEDO巡回。数分。連続実行はrate limitに注意）
- プレビュー検証は `.claude/launch.json`（gitignore）でも可。`preview_screenshot`は件数多いと時々タイムアウト→`preview_eval`でDOM確認。

## 6. 必要なアカウント・認証（別アカウントで引き継ぐ場合）
- **GitHub**: コードをpushするには `repo` + `workflow` スコープの Personal Access Token が必要（`.github/workflows/` 変更には workflow 必須）。新オーナーは自分のGitHubでforkまたは移管し、自分のPATを使う。
- **GitHub Actions**: ワークフロー自体は各リポジトリの `GITHUB_TOKEN` で動くのでオーナーのPATは不要。fork/移管後は Actions タブで有効化を確認。
- **Render**: GitHub連携で Web Service を作成。Start Command=`uvicorn backend.main:app --host 0.0.0.0 --port $PORT`、Build=`pip install -r requirements.txt`、Plan=Free。push毎に自動デプロイ。
- 旧オーナーのトークンは引き継ぎ後に必ず Revoke。

## 7. 現状サマリ（2026-06-26時点）
- 情報源は **NEDOのみ・約98件**（募集中16前後/受付終了/事業者決定43）。
- 状態3分類・事業名でのプロジェクト追跡・タグ・予算規模(本文記載分)・決定事業者(HTML記載分)まで実装済み・本番反映済み。

## 8. 既知の制約 / 次にやること（優先度順）
1. **情報源を増やす**（最優先・収益化の核心）。現状NEDOのみ＝「入札」カテゴリが空。自治体・他省庁の静的HTML一覧を `scraper.py` に追加（分野ページ巡回と同じ要領）。
2. **PDF予算抽出は実装済み**（pypdf、本文に予算が無い場合のフォールバック。予算カバレッジ 6→27件/100件）。さらに精度を上げる余地あり（表現が多様、表組みは苦手）。
3. **コンソーシアム決定事業者**の社名が区切り無し連結（日本語社名の区切り推定が困難）。改善余地あり。
4. **Render無料プラン**は15分非アクセスで休止し初回表示が遅い＋稀にcold start 404。常時稼働は有料化やpingで回避。
5. ビジネス: 無料/有料プラン、アラート通知、課金導線は未実装。

## 9. 引き継ぎ後 最初の一手（推奨）
「`scraper.py` を見て、NEDO以外の静的HTML公募一覧（例: 自治体・省庁）を1つ追加し、`run_all_scrapers()` に組み込んで `build_dataset.py` でCSVに蓄積する」。データ項目（title/category/organization/prefecture/published_at/deadline/result_date/url/summary/detail/tags/source）を埋めれば、UIは自動で対応します。
