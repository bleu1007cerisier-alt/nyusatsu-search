# 入札・プロポーザル検索アプリ 引き継ぎガイド

別のClaude Code環境／アカウントで続きを行うための引き継ぎ資料です。
**まずこのファイルを最初から最後まで読んでください。**

---

## 0. このプロジェクトは何か

- 日本全国の入札・公募（プロポーザル）情報を一元検索できるWebアプリ。
- オーナーの目標：**月5万円の副収入**（有料プラン980円/月 × 50人を想定）。
- 公開中: **https://nyusatsu-search.onrender.com/**
- GitHub: **https://github.com/bleu1007cerisier-alt/nyusatsu-search**

---

## 1. システム構成（全体像）

```
【収集層】毎日3回自動実行（GitHub Actions cron）
  NEDO公式サイト
    ↓ HTML巡回・PDF取得
  GitHub Actions (scrape.yml → build_dataset.py + scraper.py)
    ↓ PDF保存             ↓ CSVコミット
  Cloudflare R2        GitHub (dataset/tenders.csv) ← データの正本
                           ↓ push検知
                       Render.com 自動デプロイ

【表示層】リクエスト時
  ユーザーブラウザ → Render.com (FastAPI) → SQLite（起動時にCSVから構築）
                                          ↑ 実行時スクレイピングなし
```

**重要**: ローカルPC（あなたのPC）は開発・デバッグ専用。本番運用はローカル不要。

---

## 2. 技術スタック

| 役割 | 技術 | ファイル |
|------|------|---------|
| バックエンド | Python + FastAPI | `backend/main.py` |
| データベース | SQLite（起動時構築・永続化しない） | `backend/database.py` |
| データ正本 | CSV（GitHubに蓄積） | `dataset/tenders.csv` |
| フロントエンド | 素のHTML + Tailwind CDN | `frontend/index.html`, `frontend/detail.html` |
| スクレイピング | aiohttp + BeautifulSoup + pypdf | `backend/scraper.py` |
| データ蓄積 | build_dataset.py（Actions専用） | `scripts/build_dataset.py` |
| PDFストレージ | Cloudflare R2（S3互換） | `backend/storage.py` |
| デプロイ | Render.com 無料プラン | `render.yaml` |
| 自動更新 | GitHub Actions cron | `.github/workflows/scrape.yml` |

---

## 3. データの流れ（最重要）

### 3-1. 収集フロー（GitHub Actions が自動実行）

```
1. scrape_nedo() で NEDO分野ページを全巡回 → 案件一覧を取得
2. 既存 tenders.csv とURLキーでマージ（既存データは消さず蓄積）
3. needs_fetch() で未取得案件のみ詳細ページ取得（増分）
   - 概要(detail)未取得 → fetch_nedo_detail() で取得
   - 予算(amount)未取得 → HTML本文 → 無ければ公募要領PDFから抽出
   - R2有効かつ添付未保存 → _store_attachments() でR2にアップロード
4. R2に添付済みで予算未取得の案件 → _budget_from_r2() でR2 PDFから再抽出
5. 決定事業者(awardee)未取得 → fetch_nedo_result() で取得
6. 変更があれば tenders.csv をコミット → Render が自動デプロイ
```

### 3-2. 表示フロー（Render.com）

```
サーバー起動 → load_dataset_into_db() → tenders.csv を SQLite に全件投入
リクエスト → compute_status() で状態を動的計算 → JSON返却
※ 起動後のスクレイピングは一切しない（軽量・安定）
```

---

## 4. CSVの構造（dataset/tenders.csv）

文字コード: **utf-8-sig**（BOM付き）。`id`列が安定キー。

| 列名 | 内容 | 備考 |
|------|------|------|
| id | 連番（整数） | マージ時に自動採番 |
| title | 業務名 | 【P25011】等の事業コードは除去済み |
| category | 入札 / プロポーザル | 現状ほぼプロポーザル |
| organization | 発注機関名 | |
| prefecture | 都道府県 / 国 | |
| published_at | 公募開始日（YYYY-MM-DD） | |
| deadline | 締切日（YYYY-MM-DD） | |
| result_date | 事業者決定日（YYYY-MM-DD） | あれば事業者決定ステータス |
| project_code | 事業コード（P25011等） | NEDOの予算番号。別テーマも同番号になるため関連案件のまとめには使わない |
| awardee | 決定事業者名 | HTMLに記載がある案件のみ |
| awardee_checked | 1=確認済み | 再取得しない制御フラグ |
| amount | 予算規模（万円表記） | 例: `2,000万円以内` `1件あたり1,500万円程度` |
| budget_checked | 1=確認済み | 再取得しない制御フラグ |
| url | 公募詳細ページURL | |
| summary | AI要約文（300〜500文字） | AI未設定時は分野名等の短いテキストが入る場合あり |
| summary_checked | 1=AI要約済み | 空=未要約。次回AI有効実行時に detail から自動要約される |
| detail | 概要文（業務内容・生テキスト） | 連絡先・メール除外済み。AI要約の原文 |
| schedule | 予定リスト（JSON） | `[{label, date, raw}]` |
| attachments | R2保存情報（JSON） | `[{name, kind, url, key, source_url}]` |
| attachments_checked | 1=確認済み | 再アップロードしない制御フラグ |
| tags | タグ（カンマ区切り） | TAG_KEYWORDS + NEDO分野名 |
| source | データソース名 | 現状 "NEDO" |
| first_seen | 初回取得日 | |
| last_seen | 最終確認日 | |

---

## 5. 重要な実装ポイント

### 5-1. 状態(status)はDB非保存・動的計算
```python
# backend/main.py の compute_status()
result_date あり → 事業者決定
deadline < 今日  → 受付終了
それ以外         → 募集中
```

### 5-2. 関連案件は title 完全一致で束ねる
`project_code`（予算番号）は別テーマの公募も同番号にぶら下がるため使わない。

### 5-3. 予算抽出の優先順位
```
HTML本文「予算規模：」→ 公募要領PDF（NEDOから直接DL）→ R2保存PDF（_budget_from_r2）
```
抽出パターン: 予算規模 / 上限額 / 委託費 / 委託業務費 / 上限金額 / 交付上限額 / 補助上限額 等

### 5-4. R2ストレージのキー形式
```
nedo/{published_at}_{id}/{kind}_{filename}
例: nedo/2025-04-01_42/公募要領_koubo.pdf
```

### 5-5. 増分更新フラグの仕組み
- `budget_checked=1` → 詳細ページ取得済み（予算が空でも再取得しない）
- `awardee_checked=1` → 決定事業者確認済み
- `attachments_checked=1` → R2保存済み
- `summary_checked=1` → AI要約済み（空=未要約。ANTHROPIC_API_KEY有効時に detail から自動要約）

**フラグを空にリセット → 次回実行時に再取得・再生成される**

> **AI要約のフロー：**
> needs_fetch()対象案件 → 詳細取得 → summary_checked未設定なら要約
> needs_fetch()対象外（budget_checked=1済み）→ 別ループで detail から要約
> いずれも summary_checked=1 が立ったら以後スキップ（コスト重複なし）

### 5-6. キャッシュ防止
全APIレスポンスに `Cache-Control: no-store` ヘッダを付与（`add_no_cache_headers` ミドルウェア）。

---

## 6. 現状サマリ（2026-06-27時点）

| 項目 | 値 |
|------|-----|
| データ件数 | 100件（NEDOのみ） |
| 予算カバレッジ | 76/100件（76%） |
| R2保存PDF | 203ファイル / 113MB |
| 状態内訳 | 募集中 約16件 / 受付終了 約41件 / 事業者決定 約43件 |
| スクレイピング頻度 | 毎日3回（9:30/13:30/15:30 JST）|

---

## 7. インフラ・認証情報

### GitHub Secrets（Actions実行時に自動注入）
| Secret名 | 用途 |
|---------|------|
| `R2_ENDPOINT` | `https://{accountid}.r2.cloudflarestorage.com` |
| `R2_ACCESS_KEY_ID` | Cloudflare R2 APIトークン |
| `R2_SECRET_ACCESS_KEY` | Cloudflare R2 シークレット |
| `R2_BUCKET` | `nyusatsu-docs` |

`R2_PUBLIC_URL` は未設定（PDFは非公開。サイトには原文リンクを表示）。

### ローカル開発用 .env（gitignore済み・GitHub未登録）
```
R2_ENDPOINT=https://bbb8a2c9b518b16ec1969f232154d2c4.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=（オーナーに確認）
R2_SECRET_ACCESS_KEY=（オーナーに確認）
R2_BUCKET=nyusatsu-docs
```

### GitHub PAT（コードpush・Actions手動実行用）
- スコープ: `repo` + `workflow`
- トークン名: `nyusatsu-deploy4`（有効期限: 2026-09-27）
- 保存場所: ローカルの `.env` ファイル（`GITHUB_PAT=ghp_...`）
- Actions手動実行（PowerShell）:
  ```powershell
  # .envからトークンを読み込んでトリガー
  $env = Get-Content .env | Where-Object { $_ -match "^GITHUB_PAT=" }
  $token = $env -replace "^GITHUB_PAT=", ""
  $headers = @{ Authorization = "token $token"; Accept = "application/vnd.github.v3+json" }
  Invoke-RestMethod -Method POST -Uri "https://api.github.com/repos/bleu1007cerisier-alt/nyusatsu-search/actions/workflows/scrape.yml/dispatches" -Headers $headers -Body '{"ref":"main"}' -ContentType "application/json"
  ```

### Render.com
- Start Command: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`（**$PORT必須**）
- Build Command: `pip install -r requirements.txt`
- プラン: Free（15分無アクセスで休止→初回表示遅い）

---

## 8. ローカル開発手順

```bash
git clone https://github.com/bleu1007cerisier-alt/nyusatsu-search.git
cd nyusatsu-search
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
```

**サーバー起動（表示確認用）:**
```bash
.venv/Scripts/python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8130
# → http://127.0.0.1:8130/
```

**手動スクレイピング（R2有効にするには.envが必要）:**
```bash
.venv/Scripts/python.exe scripts/build_dataset.py
# 数分かかる。連続実行はNEDOのrate limitに注意
```

**R2の内容確認（.envが必要）:**
```python
import os, boto3
# .envを読み込んでからboto3で接続
s3 = boto3.client('s3', endpoint_url=os.environ['R2_ENDPOINT'], ...)
resp = s3.list_objects_v2(Bucket='nyusatsu-docs')
```

---

## 9. ファイル構成（主要ファイルのみ）

```
nyusatsu-search/
├── backend/
│   ├── main.py          # FastAPI アプリ本体・API定義
│   ├── database.py      # SQLite モデル定義・マイグレーション
│   ├── scraper.py       # スクレイピング関数群（NEDO対応）
│   └── storage.py       # Cloudflare R2 アップロード
├── frontend/
│   ├── index.html       # 一覧ページ
│   └── detail.html      # 詳細ページ
├── scripts/
│   └── build_dataset.py # データ蓄積スクリプト（Actions専用）
├── dataset/
│   └── tenders.csv      # ★データの正本（utf-8-sig）
├── .github/workflows/
│   └── scrape.yml       # GitHub Actions cron定義
├── requirements.txt     # Python依存パッケージ
├── render.yaml          # Render.com デプロイ設定
└── .env                 # ★gitignore済み・R2鍵（ローカルのみ）
```

---

## 10. 既知の制約・次にやること（優先度順）

1. **情報源を増やす**（最優先・収益化の核心）
   - 現状NEDOのみ → 「入札」カテゴリが空
   - 追加手順: `scraper.py` に新スクレイパー関数を書き、`run_all_scrapers()` に追加
   - 必要なデータ項目: `title, category, organization, prefecture, published_at, deadline, result_date, url, summary, detail, tags, source`
   - UIは自動対応済み（source別フィルターも動作する）

2. **PDF予算カバレッジをさらに改善**
   - 現状 76/100件（76%）
   - 未取得24件はPDFに予算記載なし or テキスト抽出不可（スキャンPDF等）
   - 表組み内の金額はpypdfで取れないケースがある

3. **コンソーシアム決定事業者**の社名連結問題
   - 複数社が連結表記されるケースで区切り推定が困難

4. **Render無料プラン**の休止問題
   - 15分無アクセスで休止→初回表示10秒程度かかる
   - 解決策: Render有料化 or 外部pingサービス（UptimeRobot等）

5. **GitHub Actions のスケジュール遅延**（2026-06-29 調査済み）
   - 設定: `00:30/04:30/06:30 UTC`（JST 9:30/13:30/15:30）の3回
   - 実績: 毎回3〜8時間遅延して実行される（GitHub無料リポジトリの仕様・混雑時にキュー待ち）
   - 「今日更新されていない」ように見えても当日中に実行されるので正常
   - 解決策が必要なら Render 側で独自 cron を設定（有料プラン）

6. **ビジネス機能**（未実装）
   - 有料プラン・課金導線・メールアラート通知・ユーザー管理

---

## 11. 引き継ぎ後 最初の一手（推奨）

```
scraper.py を開き、NEDO以外の静的HTML公募一覧（例: JST・農水省・国交省）を
1ソース追加して run_all_scrapers() に組み込む。
```

追加の流れ:
1. 対象サイトの一覧HTMLを確認（BeautifulSoupで解析できるか）
2. `scrape_xxx()` 関数を書く（scrape_nedo を参考に）
3. `run_all_scrapers()` に `scrape_xxx()` を追加
4. `source="XXX"` を必ずセット（フィルター・区別に使用）
5. ローカルで `build_dataset.py` を実行して確認
6. push → GitHub Actions で本番反映

---

## 12. 複数の作業者で作業する場合のルール

複数のClaude Code（別アカウント・別セッション）が同時並行で作業する際のルールです。

### 作業開始前に必ず実行
```bash
git pull   # 他の作業者の変更を取り込んでから始める
```

### コード変更はフィーチャーブランチで
```bash
git checkout -b feature/xxx   # 例: feature/add-jst-scraper
# 作業・コミット
git push origin feature/xxx
# → GitHubでPR作成 → mainにマージ
```
**mainへの直接pushは原則禁止**（特に複数人が同時作業している場合）。
軽微な修正（typo等）のみ直接pushを許容。

### dataset/tenders.csv の扱い
- CSVはGitHub Actionsが自動コミットする。**手動でCSVをpushする場合は必ず直前にpullしてマージ確認**。
- `budget_checked` / `awardee_checked` / `attachments_checked` フラグを手動でリセットする場合は、他の作業者に知らせること（大量のワークフロー実行時間が発生する）。

### GitHub Actionsのワークフロー実行
- 手動実行は1回ずつ。実行中に重複実行しない（CSVの競合コミットが発生する）。
- 実行状況の確認: https://github.com/bleu1007cerisier-alt/nyusatsu-search/actions

### 作業ログを残す
- コミットメッセージに「何をしたか」を明確に書く
- 大きな変更をしたらONBOARDING.mdの「現状サマリ」を更新する
- `.env` ファイルはgitignore済み。各作業者がオーナーから鍵を受け取り個別に作成する

### 作業分担の例（推奨）
| 担当 | 内容 |
|------|------|
| 作業者A | スクレイピング追加（省庁・自治体） |
| 作業者B | UI改善・フロントエンド |
| 作業者C | 予算抽出精度改善・データ品質 |

同じファイルを同時に編集しない。担当を決めてから着手すること。

---

## 13. scraper.py 主要関数リファレンス

| 関数 | 役割 |
|------|------|
| `scrape_nedo()` | NEDO一覧を巡回し案件リストを返す（async） |
| `fetch_nedo_detail(url)` | 詳細ページから概要・予算・予定・添付リンクを取得 |
| `fetch_nedo_result(url)` | 結果ページから決定事業者を取得 |
| `_extract_budget(text)` | HTMLテキストから予算を抽出 |
| `_extract_pdf_budget(text)` | PDFテキストから予算を抽出（幅広いパターン対応） |
| `_pdf_budget_from_soup(soup)` | 公募ページのPDFをDLして予算抽出 |
| `_format_amount(raw)` | 金額を万円表記に統一 |
| `_extract_overview(soup)` | 概要を抽出（業務内容優先・連絡先除外） |
| `_extract_schedule(text)` | 予定（説明会・締切等）を時系列で抽出 |
| `_extract_attachment_links(soup)` | 公募要領・仕様書等のPDFリンクを抽出 |
| `generate_tags(*texts)` | タグを自動生成 |
| `run_all_scrapers()` | 全スクレイパーを実行（async） |
