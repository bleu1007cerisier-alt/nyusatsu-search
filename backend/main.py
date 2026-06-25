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

from database import init_db, get_db, Tender
from scraper import run_all_scrapers, fetch_nedo_detail
from datetime import date

app = FastAPI(title="入札・プロポーザル検索", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# データベース初期化
init_db()


@app.on_event("startup")
async def startup_event():
    """起動時にデータがなければ自動取得"""
    db = next(get_db())
    count = db.query(Tender).count()
    db.close()
    if count == 0:
        asyncio.create_task(fetch_and_store())


async def fetch_and_store():
    """スクレイピングしてDBに保存"""
    results = await run_all_scrapers()
    db = next(get_db())
    try:
        for item in results:
            existing = db.query(Tender).filter(Tender.url == item["url"], Tender.title == item["title"]).first()
            if not existing:
                tender = Tender(**item)
                db.add(tender)
        db.commit()
    finally:
        db.close()
    return len(results)


def _tag_list(t: Tender):
    return [x for x in (t.tags or "").split(",") if x]


@app.get("/api/tenders")
def search_tenders(
    q: Optional[str] = Query(None, description="キーワード検索"),
    category: Optional[str] = Query(None, description="入札 or プロポーザル"),
    prefecture: Optional[str] = Query(None, description="都道府県"),
    source: Optional[str] = Query(None, description="データソース"),
    tag: Optional[str] = Query(None, description="タグ"),
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

    total = query.count()
    # 並び順：①受付中（締切が今日以降）を先頭に、締切が近い順 ②受付終了を新しい順 ③締切未定は最後
    today = date.today().isoformat()
    open_q = query.filter(Tender.deadline >= today)
    closed_q = query.filter(and_(Tender.deadline != "", Tender.deadline < today))
    undated_q = query.filter(Tender.deadline == "")
    ordered = (
        open_q.order_by(Tender.deadline.asc()).all()
        + closed_q.order_by(Tender.deadline.desc()).all()
        + undated_q.order_by(Tender.published_at.desc()).all()
    )
    items = ordered[skip:skip + limit]

    return {
        "total": total,
        "items": [
            {
                "id": t.id,
                "title": t.title,
                "category": t.category,
                "organization": t.organization,
                "prefecture": t.prefecture,
                "deadline": t.deadline,
                "published_at": t.published_at,
                "amount": t.amount,
                "url": t.url,
                "summary": t.summary,
                "source": t.source,
                "tags": _tag_list(t),
            }
            for t in items
        ],
    }


@app.get("/api/tenders/{tender_id}")
def get_tender(tender_id: int, db: Session = Depends(get_db)):
    """1件の詳細を返す（アプリ内詳細表示用）。"""
    t = db.query(Tender).filter(Tender.id == tender_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="該当する案件が見つかりません")

    # 概要が未取得のNEDO案件は、この時点で詳細ページから取得して保存する（遅延取得）
    if t.source == "NEDO" and not t.detail and t.url:
        info = fetch_nedo_detail(t.url)
        if info.get("detail"):
            t.detail = info["detail"]
        # 一覧の締切より詳細ページの締切の方が正確な場合は更新（未来日のみ採用）
        dl = info.get("deadline")
        if dl and (not t.deadline or dl >= (t.published_at or "")):
            t.deadline = dl
        db.commit()
        db.refresh(t)

    return {
        "id": t.id,
        "title": t.title,
        "category": t.category,
        "organization": t.organization,
        "prefecture": t.prefecture,
        "deadline": t.deadline,
        "published_at": t.published_at,
        "amount": t.amount,
        "url": t.url,
        "summary": t.summary,
        "detail": t.detail,
        "source": t.source,
        "tags": _tag_list(t),
    }


@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    total = db.query(Tender).count()
    nyusatsu = db.query(Tender).filter(Tender.category == "入札").count()
    proposal = db.query(Tender).filter(Tender.category == "プロポーザル").count()
    sources = db.query(Tender.source).distinct().all()
    prefectures = db.query(Tender.prefecture).distinct().all()

    # タグを集計（件数の多い順）
    tag_counts: dict = {}
    for (tags_str,) in db.query(Tender.tags).all():
        for tag in (tags_str or "").split(","):
            tag = tag.strip()
            if tag:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
    top_tags = sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True)

    return {
        "total": total,
        "nyusatsu": nyusatsu,
        "proposal": proposal,
        "sources": [s[0] for s in sources if s[0]],
        "prefectures": [p[0] for p in prefectures if p[0]],
        "tags": [{"name": name, "count": cnt} for name, cnt in top_tags],
    }


@app.post("/api/refresh")
async def refresh_data(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    background_tasks.add_task(fetch_and_store)
    return {"message": "データ取得を開始しました"}


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
