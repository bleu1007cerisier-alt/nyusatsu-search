from fastapi import FastAPI, Depends, Query, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_
from typing import Optional, List
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import init_db, get_db, Tender, SessionLocal
from datetime import date
import csv
import json

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
STATUS_CLOSED = "受付終了"
STATUS_DECIDED = "事業者決定"


def load_dataset_into_db() -> int:
    """蓄積済みCSV（dataset/tenders.csv）をDBへ読み込む。サイト側はスクレイピングしない。"""
    if not os.path.exists(DATASET_CSV):
        return 0
    db = SessionLocal()
    try:
        db.query(Tender).delete()
        with open(DATASET_CSV, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                db.add(Tender(
                    id=int(row["id"]) if (row.get("id") or "").strip().isdigit() else None,
                    title=row.get("title", ""),
                    category=row.get("category", ""),
                    organization=row.get("organization", ""),
                    prefecture=row.get("prefecture", ""),
                    published_at=row.get("published_at", ""),
                    deadline=row.get("deadline", ""),
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
    """状態を判定：結果あり→事業者決定、締切超過→受付終了、それ以外→募集中。
    締切が空欄の場合は掲載日から180日超で受付終了とみなす。"""
    if (t.result_date or "").strip():
        return STATUS_DECIDED
    if (t.deadline or "").strip() and t.deadline < today:
        return STATUS_CLOSED
    # PORTALでdeadline・detail両方空 = ページ削除済みと判断して受付終了
    if not (t.deadline or "").strip() and not (t.detail or "").strip() and (t.source or "") == "PORTAL":
        return STATUS_CLOSED
    if not (t.deadline or "").strip() and (t.published_at or "").strip():
        from datetime import date as _date, timedelta
        try:
            pub = _date.fromisoformat(t.published_at[:10])
            if (_date.fromisoformat(today) - pub).days > 180:
                return STATUS_CLOSED
        except ValueError:
            pass
    return STATUS_OPEN


def _status_rank(status: str) -> int:
    return {STATUS_OPEN: 0, STATUS_CLOSED: 1, STATUS_DECIDED: 2}.get(status, 3)


def _sort_key(item):
    """募集中(締切近い順)→受付終了・事業者決定(掲載日新しい順、区別なし)。"""
    st = item["status"]
    if st == STATUS_OPEN:
        return (0, _rev(item["published_at"] or item["deadline"] or ""))
    return (1, _rev(item["published_at"] or item["deadline"] or ""))


def _rev(s: str) -> str:
    """文字列を降順ソートするためのキー（各文字を反転）。"""
    return "".join(chr(255 - ord(c)) for c in s) if s else "\xff" * 10


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
    q: Optional[str] = Query(None, description="キーワード検索"),
    category: Optional[str] = Query(None, description="入札 or プロポーザル"),
    prefecture: Optional[str] = Query(None, description="都道府県"),
    organization: Optional[str] = Query(None, description="発注機関（府省庁等）"),
    source: Optional[str] = Query(None, description="データソース"),
    tag: Optional[str] = Query(None, description="タグ"),
    status: Optional[str] = Query(None, description="募集中 / 受付終了 / 事業者決定"),
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    query = db.query(Tender)

    if q:
        query = query.filter(
            or_(
                Tender.title.contains(q),
                Tender.organization.contains(q),
                Tender.summary.contains(q),
                Tender.detail.contains(q),
                Tender.tags.contains(q),
            )
        )
    if category:
        query = query.filter(Tender.category == category)
    if prefecture:
        query = query.filter(Tender.prefecture == prefecture)
    if organization:
        query = query.filter(Tender.organization == organization)
    if source:
        query = query.filter(Tender.source == source)
    if tag:
        query = query.filter(Tender.tags.contains(tag))

    today = date.today().isoformat()
    items = [_item_dict(t, today) for t in query.all()]

    # 状態フィルタ
    if status in (STATUS_OPEN, STATUS_CLOSED, STATUS_DECIDED):
        items = [i for i in items if i["status"] == status]

    items.sort(key=_sort_key)
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
    my_tags = set(_tag_list(t))
    similar = []
    if my_tags:
        related_titles = {r["title"] for r in related} | {t.title}
        for s in db.query(Tender).filter(Tender.id != t.id).all():
            if s.title in related_titles:
                continue
            overlap = my_tags & set(_tag_list(s))
            if overlap:
                similar.append((len(overlap), s))
        # 一致数の多い順→受付中優先→新しい順
        def _rank(pair):
            n, s = pair
            st = compute_status(s, today)
            return (-n, 0 if st == STATUS_OPEN else 1, _rev(s.published_at or ""))
        similar.sort(key=_rank)
        data["similar"] = [
            {
                "id": s.id, "title": s.title, "status": compute_status(s, today),
                "deadline": s.deadline, "tags": _tag_list(s),
                "match": n,
            }
            for n, s in similar[:5]
        ]
    else:
        data["similar"] = []
    return data


@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    today = date.today().isoformat()
    all_items = db.query(Tender).all()
    total = len(all_items)

    status_counts = {STATUS_OPEN: 0, STATUS_CLOSED: 0, STATUS_DECIDED: 0}
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

    return {
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


# 参照しているデータソース（スクレイピング対象サイト）
DEV_SOURCES = [
    {"code": "PORTAL", "label": "調達ポータル",
     "url": "https://www.p-portal.go.jp/pps-web-biz/", "desc": "各府省庁の入札・公募"},
    {"code": "NEDO", "label": "NEDO",
     "url": "https://www.nedo.go.jp/koubo/", "desc": "新エネルギー・産業技術総合開発機構の公募"},
    {"code": "JST", "label": "JST",
     "url": "https://www.jst.go.jp/", "desc": "科学技術振興機構の公募"},
]


@app.get("/api/dev/status")
def dev_status():
    """開発者ページ用：自動更新履歴・データソースの取得状況・AIコスト推定を返す。"""
    today = date.today().isoformat()

    # ソース別の取得状況をCSVから集計（last_seen はDB未保持のためCSVを直接読む）
    by_source = {}
    total = 0
    summarized = 0          # AI要約が入っている件数
    summary_eligible = 0    # 本文(detail)があり要約対象になりうる件数
    if os.path.exists(DATASET_CSV):
        with open(DATASET_CSV, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                total += 1
                s = row.get("source") or "?"
                d = by_source.setdefault(s, {"count": 0, "last_seen": "", "open": 0})
                d["count"] += 1
                ls = (row.get("last_seen") or "")
                if ls > d["last_seen"]:
                    d["last_seen"] = ls
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

    sources = []
    for src in DEV_SOURCES:
        info = by_source.get(src["code"], {"count": 0, "last_seen": "", "open": 0})
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
        })

    # 自動更新履歴・AIコスト
    log_path = os.path.join(os.path.dirname(__file__), "../dataset/update_log.json")
    runs, cost_recent = [], 0.0
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            runs = data.get("runs", [])
            cost_recent = data.get("cumulative_cost_usd_recent", 0.0)
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
        "ai_model": "claude-haiku-4-5",
        "console_url": "https://console.anthropic.com/settings/billing",
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
