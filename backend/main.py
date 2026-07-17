from fastapi import FastAPI, Depends, Query, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, defer
from sqlalchemy import or_, and_
from typing import Optional, List
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import init_db, get_db, Tender, SessionLocal
from tag_master import TAG_MASTER
from datetime import date, timedelta
import csv
import json
import re

app = FastAPI(title="入札・プロポーザル検索", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    """ブラウザに古いデータをキャッシュさせない（常に最新を表示する）。"""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


# データベース初期化
init_db()

DATASET_CSV = os.path.join(os.path.dirname(__file__), "../dataset/tenders.csv")
STATUS_OPEN = "募集中"
STATUS_PUBLIC = "公開中"
STATUS_ENDED = "公開終了"


# /api/stats・一覧の結果キャッシュ（DB再読込・日付変更で無効化）
_STATS_CACHE: dict = {"date": None, "data": None}
_ITEMS_CACHE: dict = {"date": None, "items": None}


def load_dataset_into_db() -> int:
    """蓄積済みCSV（dataset/tenders.csv）をDBへ読み込む。サイト側はスクレイピングしない。"""
    global _STATS_CACHE, _ITEMS_CACHE
    _STATS_CACHE = {"date": None, "data": None}  # データ更新でキャッシュ無効化
    _ITEMS_CACHE = {"date": None, "items": None}
    if not os.path.exists(DATASET_CSV):
        return 0
    db = SessionLocal()
    try:
        db.query(Tender).delete()
        with open(DATASET_CSV, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        # ID割当：既存の数値IDを保持しつつ、空・非数値・重複には一意IDを採番する。
        # （空IDや重複IDがあってもDB投入がUNIQUE制約で失敗しないようにする防御策）
        assigned = set()
        for r in rows:
            v = (r.get("id") or "").strip()
            if v.isdigit():
                assigned.add(int(v))
        next_id = (max(assigned) + 1) if assigned else 1
        used = set()

        def _alloc(v):
            nonlocal next_id
            if v.isdigit() and int(v) not in used:
                used.add(int(v))
                return int(v)
            while next_id in assigned or next_id in used:
                next_id += 1
            used.add(next_id)
            return next_id

        for row in rows:
                db.add(Tender(
                    id=_alloc((row.get("id") or "").strip()),
                    title=row.get("title", ""),
                    category=row.get("category", ""),
                    organization=row.get("organization", ""),
                    prefecture=row.get("prefecture", ""),
                    published_at=row.get("published_at", ""),
                    deadline=row.get("deadline", ""),
                    close_date=row.get("close_date", ""),
                    result_date=row.get("result_date", ""),
                    project_code=row.get("project_code", ""),
                    awardee=row.get("awardee", ""),
                    amount=row.get("amount", ""),
                    url=row.get("url", ""),
                    source_category=row.get("source_category", ""),
                    summary=row.get("summary", ""),
                    detail=row.get("detail", ""),
                    schedule=row.get("schedule", ""),
                    attachments=row.get("attachments", ""),
                    tags=row.get("tags", ""),
                    source=row.get("source", ""),
                ))
        db.commit()
        return db.query(Tender).count()
    finally:
        db.close()


@app.on_event("startup")
def startup_event():
    """起動時に蓄積済みCSVを読み込む（スクレイピングは行わない）。"""
    n = load_dataset_into_db()
    print(f"データセット読み込み: {n}件")


def compute_status(t: Tender, today: str) -> str:
    """状態を判定。
    - 公開終了: close_date ≤ today（掲載期間終了）
    - 募集中:   deadline ≥ today（入札受付中）
    - 公開中:   それ以外（掲載中だが受付終了、またはdeadline不明）
    """
    close = (t.close_date or "").strip()
    deadline = (t.deadline or "").strip()

    if close and close < today:
        return STATUS_ENDED
    if deadline and deadline >= today:
        return STATUS_OPEN
    # deadline なし・不明の場合: close_date があれば公開中、なければ公開終了
    if not deadline:
        if close and close >= today:
            return STATUS_PUBLIC
        # close_date も不明 → published_at から180日超で公開終了とみなす
        if (t.published_at or "").strip():
            from datetime import date as _date
            try:
                pub = _date.fromisoformat(t.published_at[:10])
                if (_date.fromisoformat(today) - pub).days > 180:
                    return STATUS_ENDED
            except ValueError:
                pass
        return STATUS_PUBLIC
    # deadline < today（受付終了）
    if close and close >= today:
        return STATUS_PUBLIC   # ポータルページはまだ公開中
    return STATUS_ENDED


def _status_rank(status: str) -> int:
    return {STATUS_OPEN: 0, STATUS_PUBLIC: 1, STATUS_ENDED: 2}.get(status, 3)


def _sort_key(item):
    """募集中(掲載日新しい順)→公開中→公開終了(掲載日新しい順)。"""
    st = item["status"]
    rank = _status_rank(st)
    return (rank, _rev(item["published_at"] or item["deadline"] or ""))


def _rev(s: str) -> str:
    """文字列を降順ソートするためのキー（各文字を反転）。"""
    return "".join(chr(255 - ord(c)) for c in s) if s else "\xff" * 10


def _get_sorted_items(db: Session, today: str) -> list:
    """全案件を軽量dict化してソート済みでキャッシュし、以降は使い回す。
    detail 本文は含めない（一覧・検索に不要で重いため）。"""
    global _ITEMS_CACHE
    if _ITEMS_CACHE.get("items") is not None and _ITEMS_CACHE.get("date") == today:
        return _ITEMS_CACHE["items"]
    rows = db.query(Tender).options(defer(Tender.detail)).all()
    items = [_item_dict(t, today) for t in rows]
    items.sort(key=_sort_key)
    _ITEMS_CACHE = {"date": today, "items": items}
    return items


def _tag_list(t: Tender):
    return [x for x in (t.tags or "").split(",") if x]


def _item_dict(t: Tender, today: str) -> dict:
    return {
        "id": t.id,
        "title": t.title,
        "category": t.category,
        "organization": t.organization,
        "prefecture": t.prefecture,
        "deadline": t.deadline,
        "close_date": t.close_date,
        "published_at": t.published_at,
        "result_date": t.result_date,
        "project_code": t.project_code,
        "awardee": t.awardee,
        "status": compute_status(t, today),
        "amount": t.amount,
        "url": t.url,
        "source_category": t.source_category,
        "summary": t.summary,
        "source": t.source,
        "tags": _tag_list(t),
    }


@app.get("/api/tenders")
def search_tenders(
    q: Optional[str] = Query(None, description="キーワード検索（スペース区切りでAND検索）"),
    category: Optional[str] = Query(None, description="入札 or プロポーザル"),
    prefecture: Optional[str] = Query(None, description="都道府県"),
    organization: Optional[str] = Query(None, description="発注機関（府省庁等）"),
    source: Optional[str] = Query(None, description="データソース"),
    tag: Optional[str] = Query(None, description="タグ（単一・後方互換）"),
    tags: Optional[str] = Query(None, description="タグ（カンマ区切りで複数指定）"),
    tag_mode: str = Query("or", description="複数タグの結合: or / and"),
    status: Optional[str] = Query(None, description="募集中 / 公開中 / 公開終了"),
    sort: Optional[str] = Query(None, description="並び順: deadline(締切が近い順) / new(新着順)"),
    due_within: Optional[int] = Query(None, ge=1, le=90, description="締切までの日数で絞る（募集中のみ）"),
    deadline_from: Optional[str] = Query(None, description="締切日の下限 YYYY-MM-DD"),
    deadline_to: Optional[str] = Query(None, description="締切日の上限 YYYY-MM-DD"),
    published_from: Optional[str] = Query(None, description="掲載日の下限 YYYY-MM-DD"),
    published_to: Optional[str] = Query(None, description="掲載日の上限 YYYY-MM-DD"),
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    today = date.today().isoformat()
    # 全件をメモリにキャッシュ（ソート済み）。以降はメモリ上で絞り込み→スライスし、
    # リクエストごとの全件DB読み込み・dict化・ソートを避ける（一覧表示を高速化）。
    items = _get_sorted_items(db, today)

    if q:
        # スペース（全角含む）区切りの全キーワードを含むものだけ残す（AND検索）
        tokens = [t for t in re.split(r"[\s　]+", q.lower()) if t]
        if tokens:
            def _haystack(i):
                return ((i["title"] or "") + " " + (i["organization"] or "") + " "
                        + (i["summary"] or "") + " " + " ".join(i["tags"])).lower()
            items = [i for i in items
                     if (lambda h: all(tok in h for tok in tokens))(_haystack(i))]
    if category:
        items = [i for i in items if i["category"] == category]
    if prefecture:
        items = [i for i in items if i["prefecture"] == prefecture]
    if organization:
        items = [i for i in items if i["organization"] == organization]
    if source:
        items = [i for i in items if i["source"] == source]
    if tag:
        items = [i for i in items if tag in i["tags"]]
    if tags:
        # 複数タグ検索: 実務者が関心タグを組み合わせて案件を探す（サイトの核機能）
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            if tag_mode == "and":
                items = [i for i in items
                         if all(t in i["tags"] for t in tag_list)]
            else:  # or（既定）
                items = [i for i in items
                         if any(t in i["tags"] for t in tag_list)]
    if status in (STATUS_OPEN, STATUS_PUBLIC, STATUS_ENDED):
        items = [i for i in items if i["status"] == status]
    if due_within:
        # 締切間近: 募集中かつ deadline が today〜today+N日 のもの
        limit_date = (date.fromisoformat(today) + timedelta(days=due_within)).isoformat()
        items = [i for i in items
                 if i["status"] == STATUS_OPEN and i["deadline"]
                 and today <= i["deadline"] <= limit_date]
    # 詳細検索: 締切日・掲載日の期間指定
    if deadline_from:
        items = [i for i in items if i["deadline"] and i["deadline"] >= deadline_from]
    if deadline_to:
        items = [i for i in items if i["deadline"] and i["deadline"] <= deadline_to]
    if published_from:
        items = [i for i in items if i["published_at"] and i["published_at"] >= published_from]
    if published_to:
        items = [i for i in items if i["published_at"] and i["published_at"] <= published_to]

    # 並び替え（キャッシュ共有リストを壊さないよう sorted() で新リストを作る）
    if sort == "deadline":
        # 募集中を先頭に、締切が近い順（締切なしは最後）
        items = sorted(items, key=lambda i: (_status_rank(i["status"]),
                                             i["deadline"] or "9999-12-31"))
    elif sort == "new":
        # 掲載日が新しい順
        items = sorted(items, key=lambda i: _rev(i["published_at"] or ""))

    total = len(items)
    page = items[skip:skip + limit]
    return {"total": total, "items": page}


@app.get("/api/tenders/{tender_id}")
def get_tender(tender_id: int, db: Session = Depends(get_db)):
    """1件の詳細を返す。同一事業コードの関連案件（公募→決定の経過）も付与する。"""
    t = db.query(Tender).filter(Tender.id == tender_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="該当する案件が見つかりません")
    today = date.today().isoformat()

    data = _item_dict(t, today)
    data["detail"] = t.detail
    try:
        data["schedule"] = json.loads(t.schedule) if (t.schedule or "").strip() else []
    except (ValueError, TypeError):
        data["schedule"] = []
    try:
        data["attachments"] = json.loads(t.attachments) if (t.attachments or "").strip() else []
    except (ValueError, TypeError):
        data["attachments"] = []

    # 同じ事業名（正式名称が一致するもの）の公募回をまとめる。
    # ※ 事業コードは予算番号で別テーマの公募も含むため、正式名称で厳密に同一案件のみを束ねる。
    related = []
    if (t.title or "").strip():
        siblings = db.query(Tender).filter(Tender.title == t.title).all()
        if len(siblings) > 1:
            for s in siblings:
                related.append({
                    "id": s.id,
                    "title": s.title,
                    "status": compute_status(s, today),
                    "published_at": s.published_at,
                    "deadline": s.deadline,
                    "result_date": s.result_date,
                    "is_current": s.id == t.id,
                })
            # 公示日（なければ締切）で時系列に並べる
            related.sort(key=lambda r: r["published_at"] or r["deadline"] or "")
    data["related"] = related

    # テーマが近い案件（タグの一致数でスコア。自分・同名は除外）
    # キャッシュ済みの軽量リストから計算（詳細表示ごとの全件DB走査を避ける）。
    my_tags = set(_tag_list(t))
    if my_tags:
        related_titles = {r["title"] for r in related} | {t.title}
        scored = []
        for s in _get_sorted_items(db, today):
            if s["id"] == t.id or s["title"] in related_titles:
                continue
            overlap = my_tags & set(s["tags"])
            if overlap:
                scored.append((len(overlap), s))
        scored.sort(key=lambda p: (-p[0],
                                   0 if p[1]["status"] == STATUS_OPEN else 1,
                                   _rev(p[1]["published_at"] or "")))
        data["similar"] = [
            {"id": s["id"], "title": s["title"], "status": s["status"],
             "deadline": s["deadline"], "tags": s["tags"], "match": n}
            for n, s in scored[:5]
        ]
    else:
        data["similar"] = []
    return data


@app.get("/api/tags")
def get_tags(db: Session = Depends(get_db)):
    """タグマスターをカテゴリ別に返す（各タグの件数・募集中件数つき）。

    タグ検索UI（カテゴリ別タグピッカー）用。マスター定義順を保持する。
    """
    today = date.today().isoformat()
    items = _get_sorted_items(db, today)
    counts: dict = {}
    open_counts: dict = {}
    for i in items:
        for t in i["tags"]:
            counts[t] = counts.get(t, 0) + 1
            if i["status"] == STATUS_OPEN:
                open_counts[t] = open_counts.get(t, 0) + 1
    categories = []
    for cat, tags in TAG_MASTER.items():
        entries = [{"name": t,
                    "count": counts.get(t, 0),
                    "open": open_counts.get(t, 0)} for t in tags]
        categories.append({"category": cat, "tags": entries})
    return {"categories": categories}


@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    today = date.today().isoformat()
    # 統計はDB内容が変わらない限り毎回同じ。当日分をキャッシュして再計算を避ける
    # （ホーム表示のたびに全件走査するのを防ぐ）。
    global _STATS_CACHE
    if _STATS_CACHE.get("data") is not None and _STATS_CACHE.get("date") == today:
        return _STATS_CACHE["data"]

    # detail 本文は統計に不要なので読み込まない
    all_items = db.query(Tender).options(defer(Tender.detail)).all()
    total = len(all_items)

    status_counts = {STATUS_OPEN: 0, STATUS_PUBLIC: 0, STATUS_ENDED: 0}
    tag_counts: dict = {}
    org_counts: dict = {}
    sources = set()
    for t in all_items:
        status_counts[compute_status(t, today)] += 1
        if t.source:
            sources.add(t.source)
        org = (t.organization or "").strip()
        if org:
            org_counts[org] = org_counts.get(org, 0) + 1
        for tag in (t.tags or "").split(","):
            tag = tag.strip()
            if tag:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

    top_tags = sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True)
    top_orgs = sorted(org_counts.items(), key=lambda kv: kv[1], reverse=True)
    nyusatsu = sum(1 for t in all_items if t.category == "入札")
    proposal = sum(1 for t in all_items if t.category == "プロポーザル")

    data = {
        "total": total,
        "nyusatsu": nyusatsu,
        "proposal": proposal,
        "status": status_counts,
        "sources": sorted(sources),
        "tags": [{"name": name, "count": cnt} for name, cnt in top_tags],
        "organizations": [{"name": name, "count": cnt} for name, cnt in top_orgs],
        # 開発者リンクの表示可否（環境変数 DEV_PAGE_PUBLIC=0 で非表示）
        "dev_link_visible": os.environ.get("DEV_PAGE_PUBLIC", "1") != "0",
    }
    _STATS_CACHE = {"date": today, "data": data}
    return data


# 参照しているデータソース（スクレイピング対象サイト）
DEV_SOURCES = [
    {"code": "PORTAL", "label": "調達ポータル",
     "url": "https://www.p-portal.go.jp/pps-web-biz/", "desc": "各府省庁の入札・公募"},
    {"code": "NEDO", "label": "NEDO",
     "url": "https://www.nedo.go.jp/koubo/", "desc": "新エネルギー・産業技術総合開発機構の公募"},
    {"code": "JST", "label": "JST",
     "url": "https://www.jst.go.jp/", "desc": "科学技術振興機構の公募"},
    {"code": "JOGMEC", "label": "JOGMEC",
     "url": "https://www.jogmec.go.jp/", "desc": "エネルギー・金属鉱物資源機構の公募"},
    {"code": "AICHI", "label": "愛知県",
     "url": "https://www.buppin.e-aichi.jp/", "desc": "愛知県（物品等）の入札公告"},
    {"code": "TOKYO", "label": "東京都",
     "url": "https://www.my.metro.tokyo.lg.jp/business/search/?category=188514",
     "desc": "東京都の事業者募集・公募"},
    {"code": "OSAKA", "label": "大阪府",
     "url": "https://www.e-nyusatsu.pref.osaka.jp/CALS/Publish/EbController?Shori=KokokuInfo",
     "desc": "大阪府の入札公告・プロポーザル"},
    {"code": "FUKUOKA", "label": "福岡県",
     "url": "https://www.pref.fukuoka.lg.jp/bid/", "desc": "福岡県の入札・公募・プロポーザル"},
    {"code": "MIE", "label": "三重県",
     "url": "https://www.pref.mie.lg.jp/app/nyusatsu/nyusatsu/00006836/0/0/",
     "desc": "三重県の入札公告・企画提案コンペ＋電子調達(efftis)の入札予定（建設工事等）"},
    {"code": "GIFU", "label": "岐阜県",
     "url": "https://www.pref.gifu.lg.jp/bid/search/search.php?ctg[]=5&search=1",
     "desc": "岐阜県の入札公告・公募型プロポーザル"},
    {"code": "YAMANASHI", "label": "山梨県",
     "url": "https://www.pref.yamanashi.jp/shinchaku/kokoku/index.html",
     "desc": "山梨県の入札公告・公募型プロポーザル"},
    {"code": "TOYAMA", "label": "富山県",
     "url": "https://www.pref.toyama.jp/sangyou/nyuusatsu/koubo/bosyuu.html",
     "desc": "富山県の入札公告・公募型プロポーザル"},
    {"code": "NAGANO", "label": "長野県",
     "url": "https://www.pref.nagano.lg.jp/kensa/puropo-kokoku.html",
     "desc": "長野県の公募型プロポーザル＋電子入札の建設工事・測量コンサル（現在公告中）"},
    {"code": "SHIZUOKA", "label": "静岡県",
     "url": "https://www.pref.shizuoka.jp/kensei/nyusatsukobai/index.html",
     "desc": "静岡県の入札・業務委託・プロポーザル等＋電子入札の建設工事・測量コンサル"},
    {"code": "FUKUI", "label": "福井県",
     "url": "https://www.pref.fukui.lg.jp/gyosei/tetuduki/cat4502/index.html",
     "desc": "福井県の公募型プロポーザル＋電子入札の建設工事・業務委託等"},
    {"code": "NIIGATA", "label": "新潟県",
     "url": "https://www.pref.niigata.lg.jp/life/sub/8/",
     "desc": "新潟県の入札・発注・売却"},
    {"code": "ISHIKAWA", "label": "石川県",
     "url": "https://www.ep-bis.supercals.jp/ebidPPIGPublish/EjPPIj?KikanNO=1700100",
     "desc": "石川県電子入札共同システムの入札予定（物品・役務＋建設工事・測量コンサル）"},
    {"code": "TOCHIGI", "label": "栃木県",
     "url": "https://www.pref.tochigi.lg.jp/kensei/nyuusatsu/index.html",
     "desc": "栃木県の入札・公募（業務委託・公共事業・物品・その他）"},
    {"code": "CHIBA", "label": "千葉県",
     "url": "https://www.pref.chiba.lg.jp/nyuu-kei/buppin-itaku/nyuusatsukoukoku/koukoku/index.html",
     "desc": "千葉県の入札等の公告（物品・委託等の企画提案＋電子調達の建設工事・測量）"},
    {"code": "HYOGO", "label": "兵庫県",
     "url": "https://web.pref.hyogo.lg.jp/bid/bid_opn_02.html",
     "desc": "兵庫県の入札公告（委託・役務／工事・設計／その他）"},
    {"code": "KYOTO", "label": "京都府",
     "url": "https://www.pref.kyoto.jp/shinchaku/nyusatsu/index.html",
     "desc": "京都府の入札・プロポーザル情報"},
]


@app.get("/api/dev/status")
def dev_status():
    """開発者ページ用：自動更新履歴・データソースの取得状況・AIコスト推定を返す。"""
    today = date.today().isoformat()

    # 直近7日の新規判定用のしきい値日付
    try:
        _week_ago = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
    except Exception:
        _week_ago = today

    # ソース別の取得状況をCSVから集計（last_seen はDB未保持のためCSVを直接読む）
    by_source = {}
    total = 0
    summarized = 0          # AI要約が入っている件数
    summary_eligible = 0    # 本文(detail)があり要約対象になりうる件数
    tag_total = 0           # タグ総数（平均タグ数の算出用）
    tag_zero = 0            # タグ0件の案件数
    if os.path.exists(DATASET_CSV):
        with open(DATASET_CSV, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                total += 1
                s = row.get("source") or "?"
                d = by_source.setdefault(
                    s, {"count": 0, "last_seen": "", "open": 0,
                        "last_new": "", "new_7d": 0,
                        "nyusatsu": 0, "proposal": 0, "awardee": 0,
                        "attachments": 0, "summary": 0})
                d["count"] += 1
                # 星取表（何が取れているか）用の集計
                cat = (row.get("category") or "")
                if "入札" in cat:
                    d["nyusatsu"] += 1
                if "プロポーザル" in cat or "公募" in cat:
                    d["proposal"] += 1
                if (row.get("awardee") or "").strip():
                    d["awardee"] += 1
                if (row.get("attachments") or "").strip():
                    d["attachments"] += 1
                if (row.get("summary") or "").strip():
                    d["summary"] += 1
                ls = (row.get("last_seen") or "")
                if ls > d["last_seen"]:
                    d["last_seen"] = ls
                # 新規取得の指標：first_seen（初回取得日）
                fs = (row.get("first_seen") or "")
                if fs > d["last_new"]:
                    d["last_new"] = fs
                if fs and fs[:10] >= _week_ago:
                    d["new_7d"] += 1
                # 募集中＝結果未確定 かつ 締切が今日以降（または未設定）
                result_date = (row.get("result_date") or "").strip()
                deadline = (row.get("deadline") or "").strip()
                if not result_date and (not deadline or deadline >= today):
                    d["open"] += 1
                # AI要約のカバレッジ
                if len((row.get("detail") or "").strip()) >= 100:
                    summary_eligible += 1
                if (row.get("summary") or "").strip():
                    summarized += 1
                # タグの充足状況（目標: 平均3.5タグ/件）
                n_tags = len([t for t in (row.get("tags") or "").split(",")
                              if t.strip()])
                tag_total += n_tags
                if n_tags == 0:
                    tag_zero += 1

    sources = []
    for src in DEV_SOURCES:
        info = by_source.get(src["code"],
                             {"count": 0, "last_seen": "", "open": 0,
                              "last_new": "", "new_7d": 0,
                              "nyusatsu": 0, "proposal": 0, "awardee": 0,
                              "attachments": 0, "summary": 0})
        last_seen = info["last_seen"]
        healthy = False
        if last_seen[:10]:
            try:
                healthy = (date.fromisoformat(today) -
                           date.fromisoformat(last_seen[:10])).days <= 3
            except ValueError:
                healthy = False
        sources.append({
            **src,
            "count": info["count"],
            "open": info["open"],
            "last_seen": last_seen,
            "healthy": healthy,
            "last_new": info["last_new"],       # 最新の新規取得日(first_seen)
            "new_7d": info["new_7d"],           # 直近7日の新規件数
            "has_recent_new": info["new_7d"] > 0,
            # 星取表（このソースから何が取れているか）
            "nyusatsu": info["nyusatsu"],       # 入札 件数
            "proposal": info["proposal"],       # プロポーザル/公募 件数
            "awardee": info["awardee"],         # 落札者(決定事業者) 件数
            "attachments": info["attachments"], # 添付資料あり 件数
            "summary": info["summary"],         # AI要約あり 件数
        })

    # 自動更新履歴・AIコスト
    log_path = os.path.join(os.path.dirname(__file__), "../dataset/update_log.json")
    runs, cost_recent, cost_alltime = [], 0.0, 0.0
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            runs = data.get("runs", [])
            # recent: 直近50件分のみの合計（表示している実行履歴と対応する参考値）
            # alltime: 全期間の累計（50件を超えても減らない、予算管理用の正の値）
            cost_recent = data.get("cumulative_cost_usd_recent", 0.0)
            cost_alltime = data.get("cumulative_cost_usd_alltime", cost_recent)
        except (ValueError, OSError):
            pass

    # 事業者決定チェック履歴
    result_log_path = os.path.join(os.path.dirname(__file__), "../dataset/check_results_log.json")
    result_runs = []
    if os.path.exists(result_log_path):
        try:
            with open(result_log_path, "r", encoding="utf-8") as f:
                result_runs = json.load(f).get("runs", [])
        except (ValueError, OSError):
            pass

    ai_active = summarized > 0

    return {
        "total": total,
        "sources": sources,
        "runs": list(reversed(runs))[:30],
        "result_runs": list(reversed(result_runs))[:30],  # 事業者決定チェック履歴
        "ai_active": ai_active,
        "summarized": summarized,
        "summary_eligible": summary_eligible,
        "ai_cost_recent_usd": cost_recent,
        "ai_cost_alltime_usd": cost_alltime,
        "ai_model": "claude-haiku-4-5",
        "console_url": "https://console.anthropic.com/settings/billing",
        # タグ充足状況（目標: 平均3.5タグ/件。要約が埋まると自然に上がる）
        "avg_tags": round(tag_total / total, 2) if total else 0,
        "tag_zero": tag_zero,
        "tag_target": 3.5,
    }


@app.post("/api/refresh")
def refresh_data():
    """蓄積済みCSVを再読み込みする（スクレイピングはしない）。"""
    n = load_dataset_into_db()
    return {"message": f"データを再読み込みしました（{n}件）", "count": n}


@app.get("/")
def root():
    return FileResponse(os.path.join(os.path.dirname(__file__), "../frontend/index.html"))


@app.get("/tender/{tender_id}")
def tender_page(tender_id: int):
    """案件詳細ページ（独立ページ）。JS側でIDを読み取り内容を表示する。"""
    return FileResponse(os.path.join(os.path.dirname(__file__), "../frontend/detail.html"))


@app.get("/dev")
def dev_page():
    """開発者向けステータスページ。"""
    return FileResponse(os.path.join(os.path.dirname(__file__), "../frontend/dev.html"))


# フロントエンドの静的ファイルを配信
frontend_path = os.path.join(os.path.dirname(__file__), "../frontend")
if os.path.exists(frontend_path):
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")
