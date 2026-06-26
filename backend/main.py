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
                    summary=row.get("summary", ""),
                    detail=row.get("detail", ""),
                    schedule=row.get("schedule", ""),
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
    """状態を判定：結果あり→事業者決定、締切超過→受付終了、それ以外→募集中。"""
    if (t.result_date or "").strip():
        return STATUS_DECIDED
    if (t.deadline or "").strip() and t.deadline < today:
        return STATUS_CLOSED
    return STATUS_OPEN


def _status_rank(status: str) -> int:
    return {STATUS_OPEN: 0, STATUS_CLOSED: 1, STATUS_DECIDED: 2}.get(status, 3)


def _sort_key(item):
    """募集中(締切近い順)→受付終了(新しい順)→事業者決定(決定日新しい順)。"""
    st = item["status"]
    if st == STATUS_OPEN:
        return (0, item["deadline"] or "9999-99-99")
    if st == STATUS_CLOSED:
        # 新しい順 → 文字列降順のため反転
        return (1, _rev(item["deadline"] or item["published_at"] or ""))
    if st == STATUS_DECIDED:
        return (2, _rev(item["result_date"] or ""))
    return (3, "")


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
        "summary": t.summary,
        "source": t.source,
        "tags": _tag_list(t),
    }


@app.get("/api/tenders")
def search_tenders(
    q: Optional[str] = Query(None, description="キーワード検索"),
    category: Optional[str] = Query(None, description="入札 or プロポーザル"),
    prefecture: Optional[str] = Query(None, description="都道府県"),
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
    return data


@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    today = date.today().isoformat()
    all_items = db.query(Tender).all()
    total = len(all_items)

    status_counts = {STATUS_OPEN: 0, STATUS_CLOSED: 0, STATUS_DECIDED: 0}
    tag_counts: dict = {}
    sources = set()
    for t in all_items:
        status_counts[compute_status(t, today)] += 1
        if t.source:
            sources.add(t.source)
        for tag in (t.tags or "").split(","):
            tag = tag.strip()
            if tag:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

    top_tags = sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True)
    nyusatsu = sum(1 for t in all_items if t.category == "入札")
    proposal = sum(1 for t in all_items if t.category == "プロポーザル")

    return {
        "total": total,
        "nyusatsu": nyusatsu,
        "proposal": proposal,
        "status": status_counts,
        "sources": sorted(sources),
        "tags": [{"name": name, "count": cnt} for name, cnt in top_tags],
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


# フロントエンドの静的ファイルを配信
frontend_path = os.path.join(os.path.dirname(__file__), "../frontend")
if os.path.exists(frontend_path):
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")
