from sqlalchemy import create_engine, Column, Integer, String, Date, DateTime, Text, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

DATABASE_URL = "sqlite:///./data/nyusatsu.db"

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
    deadline = Column(String(50))        # 締切日
    published_at = Column(String(50))    # 公告日
    amount = Column(String(100))         # 予定価格
    url = Column(String(1000))           # 元URLリンク
    summary = Column(Text)               # 概要
    source = Column(String(100))         # データソース名
    fetched_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("idx_category", "category"),
        Index("idx_prefecture", "prefecture"),
        Index("idx_deadline", "deadline"),
    )


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
