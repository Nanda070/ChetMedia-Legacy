from sqlalchemy import create_engine, Column, String, Integer, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime, timezone
import sqlite3

SQLALCHEMY_DATABASE_URL = "sqlite:///./chetmedia.db"

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    __table_args__ = {'extend_existing': True} # Позволяет безопасно менять модель

    # Главный ID (для старых юзеров это ID дискорда, для новых будет случайный UUID)
    id = Column(String, primary_key=True, index=True)
    
    # НОВЫЕ ПОЛЯ ДЛЯ ГИБРИДНОЙ АВТОРИЗАЦИИ:
    discord_id = Column(String, unique=True, index=True, nullable=True)
    email = Column(String, unique=True, index=True, nullable=True)
    password_hash = Column(String, nullable=True)
    is_verified = Column(Boolean, default=False)
    
    username = Column(String, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_admin = Column(Boolean, default=False)
    is_blocked = Column(Boolean, default=False)
    
    media = relationship("Media", back_populates="owner")
    albums = relationship("Album", back_populates="owner")

    verify_code = Column(String, nullable=True)

class Album(Base):
    __tablename__ = "albums"
    __table_args__ = {'extend_existing': True}
    
    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    
    # ВОТ ЭТУ СТРОКУ ДОБАВЛЯЕМ:
    name = Column(String, default="Без названия")
    
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

def auto_upgrade_db():
    """Автоматическая миграция БД при старте сервера"""
    try:
        conn = sqlite3.connect("./chetmedia.db")
        cursor = conn.cursor()
        
        commands = [
            "ALTER TABLE users ADD COLUMN discord_id TEXT;",
            "ALTER TABLE users ADD COLUMN email TEXT;",
            "ALTER TABLE users ADD COLUMN password_hash TEXT;",
            "ALTER TABLE users ADD COLUMN is_verified BOOLEAN DEFAULT 0;"
            "ALTER TABLE users ADD COLUMN verify_code TEXT;"
        ]
        
        # 1. Пытаемся добавить колонки (если они уже есть - SQLite просто проигнорирует ошибку)
        for cmd in commands:
            try:
                cursor.execute(cmd)
            except sqlite3.OperationalError:
                pass 
                
        # 2. Копируем старые ID дискорда в новую колонку (чтобы старые юзеры не потеряли доступ)
        try:
            cursor.execute("UPDATE users SET discord_id = id WHERE discord_id IS NULL;")
        except Exception:
            pass
            
        conn.commit()
        conn.close()
        print("✅ [DB] База данных проверена и готова к гибридной авторизации!")
    except Exception as e:
        print(f"❌ [DB] Ошибка авто-миграции: {e}")

# Запускаем авто-апгрейд при каждом импорте database.py
auto_upgrade_db()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

