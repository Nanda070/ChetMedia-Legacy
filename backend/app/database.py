from sqlalchemy import create_engine, Column, String, Integer, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime, timezone

SQLALCHEMY_DATABASE_URL = "sqlite:///./chetmedia.db"

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, index=True)
    username = Column(String, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    media = relationship("Media", back_populates="owner")
    albums = relationship("Album", back_populates="owner")

class Album(Base):
    __tablename__ = "albums"
    __table_args__ = {'extend_existing': True}
    
    id = Column(String, primary_key=True, index=True) # ID альбома (например: a7b8c9d0)
    user_id = Column(String, ForeignKey("users.id"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime)
    
    owner = relationship("User", back_populates="albums")
    media = relationship("Media", back_populates="album", cascade="all, delete-orphan")

class Media(Base):
    __tablename__ = "media"
    __table_args__ = {'extend_existing': True}
    
    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    album_id = Column(String, ForeignKey("albums.id"), nullable=True) # Связь с альбомом (может быть пустым)
    filename = Column(String)
    content_type = Column(String)
    
    file_hash = Column(String, index=True)
    file_path = Column(String) 
    duration = Column(Integer, nullable=True)
    is_archived = Column(Integer, default=0)
    status = Column(String, default="ready")
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime)
    
    owner = relationship("User", back_populates="media")
    album = relationship("Album", back_populates="media")

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()