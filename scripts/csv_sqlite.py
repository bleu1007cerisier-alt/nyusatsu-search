# -*- coding: utf-8 -*-
"""データセットの CSV ⇄ SQLite 無損失変換。

R2にはSQLite(tenders.db)を置き、サイトはそれを直接使う。パイプラインのマージ処理は
CSVのままなので、sync_r2 が push時 CSV→DB / pull時 DB→CSV を行うための共通変換。

CSVの列順（下記 CSV_COLS）を正としてラウンドトリップする。id は整数、他は文字列。
"""
import os
import sys
import csv as _csv

_csv.field_size_limit(10 ** 7)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "backend"))

# CSVの正準列順（既存tenders.csvのヘッダと一致させる）
CSV_COLS = [
    "id", "title", "category", "organization", "prefecture", "published_at",
    "deadline", "close_date", "result_date", "project_code", "awardee",
    "awardee_checked", "amount", "budget_checked", "url", "result_url",
    "source_category", "summary", "detail", "schedule", "attachments",
    "attachments_checked", "tags", "source", "first_seen", "last_seen",
]


def csv_to_db(csv_path: str, db_path: str) -> int:
    """CSV → 新規SQLite。既存DBは作り直す。行数を返す。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import Base, Tender

    if os.path.exists(db_path):
        os.remove(db_path)
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    model_cols = {c.name for c in Tender.__table__.columns}
    Session = sessionmaker(bind=engine)
    db = Session()
    n = 0
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        batch = []
        for row in _csv.DictReader(f):
            kw = {}
            for c in CSV_COLS:
                if c not in model_cols:
                    continue
                v = row.get(c) or ""
                if c == "id":
                    v = int(v) if str(v).strip().isdigit() else None
                kw[c] = v
            batch.append(Tender(**kw))
            n += 1
            if len(batch) >= 2000:
                db.bulk_save_objects(batch)
                db.commit()
                batch = []
        if batch:
            db.bulk_save_objects(batch)
            db.commit()
    db.close()
    return n


def db_to_csv(db_path: str, csv_path: str) -> int:
    """SQLite → CSV（CSV_COLSの列順）。行数を返す。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import Tender

    engine = create_engine(f"sqlite:///{db_path}")
    Session = sessionmaker(bind=engine)
    db = Session()
    os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
    n = 0
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        for t in db.query(Tender).yield_per(2000):
            w.writerow({c: (getattr(t, c, "") if getattr(t, c, "") is not None else "")
                        for c in CSV_COLS})
            n += 1
    db.close()
    return n
