from sqlalchemy import create_engine, Column, Integer, String, Date, DateTime, Text, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import os

# DBファイルの保存先を絶対パスで決定し、フォルダが無ければ作成する
# （作業ディレクトリに依存せず、Render等の本番環境でも確実に動作させるため）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)

DATABASE_URL = f"sqlite:///{os.path.join(DATA_DIR, 'nyusatsu.db')}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Tender(Base):
    __tablename__ = "tenders"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(500), nullable=False)
    category = Column(String(50))        # 入札 or プロポーザル
    organization = Column(String(200))   # 発注機関
    prefecture = Column(String(20))      # 都道府県
    deadline = Column(String(50))        # 公募締切日
    published_at = Column(String(50))    # 公募開始日（公示日）
    result_date = Column(String(50))     # 結果（事業者決定）の日付。あれば事業者決定済み
    project_code = Column(String(100))   # 事業コード（同一プロジェクト追跡用）
    awardee = Column(String(300))        # 決定事業者（実施予定先）。分かる場合のみ
    amount = Column(String(100))         # 予算規模 / 予定価格
    url = Column(String(1000))           # 元URLリンク
    summary = Column(Text)               # 概要（短い説明・分野名）
    detail = Column(Text)                # 詳細本文（アプリ内詳細表示用・連絡先は除外）
    schedule = Column(Text)              # 予定（説明会・各種期限）JSON文字列
    tags = Column(String(500))           # タグ（カンマ区切り）
    source = Column(String(100))         # データソース名
    fetched_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("idx_category", "category"),
        Index("idx_prefecture", "prefecture"),
        Index("idx_deadline", "deadline"),
        Index("idx_project_code", "project_code"),
    )


def init_db():
    Base.metadata.create_all(bind=engine)
    # 既存DBに新カラムが無い場合は追加する（簡易マイグレーション）
    with engine.connect() as conn:
        existing = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(tenders)").fetchall()]
        migrations = [
            ("detail", "ALTER TABLE tenders ADD COLUMN detail TEXT"),
            ("tags", "ALTER TABLE tenders ADD COLUMN tags VARCHAR(500)"),
            ("result_date", "ALTER TABLE tenders ADD COLUMN result_date VARCHAR(50)"),
            ("project_code", "ALTER TABLE tenders ADD COLUMN project_code VARCHAR(100)"),
            ("awardee", "ALTER TABLE tenders ADD COLUMN awardee VARCHAR(300)"),
            ("schedule", "ALTER TABLE tenders ADD COLUMN schedule TEXT"),
        ]
        for col, ddl in migrations:
            if col not in existing:
                conn.exec_driver_sql(ddl)
        conn.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
