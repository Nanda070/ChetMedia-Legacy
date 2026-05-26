from fastapi import FastAPI, Request, HTTPException, Depends, File, UploadFile, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import httpx
import os
import jwt
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
import asyncio

import hashlib

from contextlib import asynccontextmanager

from pydantic import BaseModel

# Импортируем SessionLocal и все модели
from app.database import get_db, SessionLocal, User, Media, Album
from app.video_processor import calculate_sha256, get_video_duration, process_and_compress_video

# Модели для массовых действий
class BulkDeleteRequest(BaseModel):
    ids: list[str]

class BulkAlbumRequest(BaseModel):
    media_ids: list[str]
    name: str

class BulkAddToAlbumRequest(BaseModel):
    media_ids: list[str]
    album_id: str

load_dotenv()

async def cleanup_expired_media_loop():
    """Фоновый цикл: раз в час проверяет базу и удаляет просроченные файлы/альбомы"""
    while True:
        await asyncio.sleep(3600) 
        print("🧹 [КЛИНЕР] Запуск автоматической очистки просроченного контента...")
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            
            # 1. Чистим одиночные файлы, у которых истек срок
            expired_solos = db.query(Media).filter(Media.expires_at < now, Media.album_id == None).all()
            deleted_solos_count = 0
            for m in expired_solos:
                count = db.query(Media).filter(Media.file_path == m.file_path).count()
                if count == 1 and m.file_path and Path(m.file_path).exists():
                    os.remove(m.file_path)
                db.delete(m)
                deleted_solos_count += 1

            # 2. Чистим просроченные альбомы целиком
            expired_albums = db.query(Album).filter(Album.expires_at < now).all()
            deleted_albums_count = 0
            for album in expired_albums:
                for m in album.media:
                    count = db.query(Media).filter(Media.file_path == m.file_path).count()
                    if count == 1 and m.file_path and Path(m.file_path).exists():
                        os.remove(m.file_path)
                db.delete(album)
                deleted_albums_count += 1
                
            if deleted_solos_count > 0 or deleted_albums_count > 0:
                db.commit()
                print(f"🗑️ [КЛИНЕР] Очистка завершена. Удалено файлов: {deleted_solos_count}, альбомов: {deleted_albums_count}")
            else:
                print("✨ [КЛИНЕР] Проверка завершена. Просроченных файлов не обнаружено.")
        except Exception as e:
            print(f"❌ [КЛИНЕР] Ошибка во время очистки: {e}")
        finally:
            db.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(cleanup_expired_media_loop())
    yield

app = FastAPI(title="ChetMedia API", lifespan=lifespan)

# ГЛОБАЛЬНЫЙ ПЕРЕХВАТЧИК ОШИБОК ДЛЯ ДИЗАЙНА СТРАНИЦ исключений
@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    if (
        request.url.path.startswith("/api") or 
        request.url.path.startswith("/media") or 
        request.url.path.startswith("/users")
    ):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    
    icon = "fa-circle-exclamation"
    if exc.status_code == 404:
        icon = "fa-triangle-exclamation"
    elif exc.status_code == 403:
        icon = "fa-user-lock"

    return templates.TemplateResponse(
        request=request, name="error.html",
        context={"request": request, "status_code": exc.status_code, "detail": exc.detail, "icon": icon},
        status_code=exc.status_code
    )

CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
REDIRECT_URI = f"{BASE_URL}/auth/callback"
JWT_SECRET = os.environ.get("JWT_SECRET")
ALGORITHM = "HS256"

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

def create_jwt_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=7)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)

def get_current_user(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("chetmedia_session")
    if not token:
        raise HTTPException(status_code=401, detail="Не авторизован")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        user = db.query(User).filter(User.id == payload.get("sub")).first()
        if not user:
            raise HTTPException(status_code=401, detail="Пользователь не найден")
        if getattr(user, "is_blocked", False):
            raise HTTPException(status_code=403, detail="Ваш аккаунт заблокирован. Пошел нахуй!")
        return user
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Недействительный токен")

async def check_admin_status(user_id: str) -> bool:
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    guild_id = os.getenv("ADMIN_GUILD_ID")
    role_id = os.getenv("ADMIN_ROLE_ID")
    if not all([bot_token, guild_id, role_id]):
        print("ВНИМАНИЕ: Не настроены переменные для админки в .env!")
        return False
    url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
    headers = {"Authorization": f"Bot {bot_token}"}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                member_data = response.json()
                return role_id in member_data.get("roles", [])
        except Exception as e:
            print(f"Ошибка проверки роли в Discord: {e}")
    return False

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})

def send_registration_log_bg(user_id: str, username: str):
    """Фоновый логгер новых регистраций в Discord"""
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    if not bot_token: return

    channel_id = "1507551870577541232" # ID канала для логов
    current_time = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M:%S")

    embed = {
        "title": "Новая регистрация",
        "color": 5763719,
        "fields": [
            {"name": "Username", "value": f"`{username}`", "inline": True},
            {"name": "ID", "value": f"`{user_id}`", "inline": True},
            {"name": "Время (UTC)", "value": current_time, "inline": False}
        ],
        "footer": {"text": "ChetMedia Security"}
    }

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}

    try:
        httpx.post(url, headers=headers, json={"embeds": [embed]})
    except Exception as e:
        print(f"❌ Ошибка отправки лога регистрации: {e}")

@app.get("/auth/login")
def login():
    discord_auth_url = f"https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&scope=identify"
    return RedirectResponse(discord_auth_url)

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("chetmedia_session")
    if not token:
        return RedirectResponse(url="/")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        user = db.query(User).filter(User.id == payload.get("sub")).first()
        if not user or not user.is_admin:
            return RedirectResponse(url="/")
    except jwt.PyJWTError:
        return RedirectResponse(url="/")
    return templates.TemplateResponse(request=request, name="admin.html", context={"request": request})

@app.get("/auth/callback")
async def callback(code: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient() as client:
        token_response = await client.post("https://discord.com/api/oauth2/token", data=data, headers=headers)
        token_data = token_response.json()
        user_response = await client.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {token_data['access_token']}"}
        )
        user_info = user_response.json()

    discord_id = user_info["id"]
    db_user = db.query(User).filter(User.id == discord_id).first()
    
    if db_user and getattr(db_user, "is_blocked", False):
        raise HTTPException(status_code=403, detail="Ваш аккаунт заблокирован. Пошел нахуй")
        
    is_user_admin = await check_admin_status(discord_id)
    if not db_user:
        db_user = User(id=discord_id, username=user_info["username"], is_admin=is_user_admin)
        db.add(db_user)
        background_tasks.add_task(send_registration_log_bg, discord_id, user_info["username"])
    else:
        db_user.is_admin = is_user_admin
        
    db.commit()
    jwt_token = create_jwt_token({"sub": discord_id, "username": user_info["username"]})
    response = RedirectResponse(url="/")
    response.set_cookie(key="chetmedia_session", value=jwt_token, httponly=True, max_age=7*24*60*60, samesite="lax")
    return response

@app.get("/users/me")
def read_users_me(current_user: User = Depends(get_current_user)):
    return {
        "message": "Ты в системе!", 
        "discord_id": current_user.id, 
        "username": current_user.username,
        "is_admin": current_user.is_admin
    }

def send_discord_dm(user_id: str, message_content: str):
    """Отправляет приватное сообщение (DM) пользователю в Discord от имени бота"""
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    if not bot_token:
        print("❌ [БОТ] Ошибка: DISCORD_BOT_TOKEN не найден в переменных окружения .env")
        return

    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json"
    }

    with httpx.Client() as client:
        try:
            # 1. Открываем приватный DM-канал с пользователем
            dm_channel_response = client.post(
                "https://discord.com/api/v10/users/@me/channels",
                json={"recipient_id": user_id},
                headers=headers
            )
            if dm_channel_response.status_code != 200:
                print(f"❌ [БОТ] Не удалось создать DM-канал с пользователем {user_id}: {dm_channel_response.text}")
                return

            channel_id = dm_channel_response.json()["id"]

            # 2. Отправляем отформатированный текст в созданный канал
            message_response = client.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                json={"content": message_content},
                headers=headers
            )
            if message_response.status_code == 200:
                print(f"📩 [БОТ] Уведомление о готовности видео успешно доставлено юзеру {user_id}")
            else:
                print(f"❌ [БОТ] Ошибка отправки сообщения в созданный канал: {message_response.text}")
                
        except Exception as e:
            print(f"❌ [БОТ] Критическое исключение при отправке DM: {e}")

def process_video_background(file_id: str, temp_path: Path, final_path: Path):
    db = SessionLocal()
    try:
        print(f"⚙️ [ФОН] Начинаем сжимать видео {file_id}...")
        success = process_and_compress_video(temp_path, final_path)
        media = db.query(Media).filter(Media.id == file_id).first()
        
        if media:
            if success:
                duration = get_video_duration(final_path)
                media.status = "ready"
                media.duration = duration
                
                now = datetime.now(timezone.utc)
                days_limit = 30 if duration <= 15 * 60 else 14
                media.expires_at = now + timedelta(days=days_limit)
                
                # Сначала фиксируем изменения в БД, чтобы ссылка гарантированно стала рабочей
                db.commit()
                print(f"✅ [ФОН] Видео {file_id} готово!")

                # --- МОДУЛЬ АВТОМАТИЧЕСКОГО УВЕДОМЛЕНИЯ В ЛС ДИСКОРДА ---
                video_url = f"{BASE_URL}/v/{file_id}"
                expires_str = media.expires_at.strftime("%Y-%m-%d %H:%M")
                
                # Используем синтаксис <@ID> для создания живого интерактивного тега в Дискорде
                notification_text = (
                    f"🎬 **ChetMedia | Обработка видео завершена!**\n\n"
                    f"Привет, <@{media.user_id}>! Твое видео успешно опубликовано на платформе.\n\n"
                    f"* **Название файла:** `{media.filename}`\n"
                    f"* **Ссылка на плеер:** {video_url}\n\n"
                    f"*⏳ Файл автоматически сгорит {expires_str} (через {days_limit} дн.).*"
                )
                
                send_discord_dm(media.user_id, notification_text)
                # --------------------------------------------------------
            else:
                media.status = "failed"
                db.commit()
                
                # Дополнительно: сообщаем пользователю, если кодек или файл сломали FFmpeg
                fail_text = f"❌ **ChetMedia | Ошибка обработки**\n\nПривет, <@{media.user_id}>. К сожалению, нам не удалось сжать твой видеофайл `{media.filename}`. Возможно, файл поврежден или использует неподдерживаемый кодек."
                send_discord_dm(media.user_id, fail_text)
                
    except Exception as e:
        print(f"❌ [ФОН] Ошибка: {e}")
    finally:
        if temp_path.exists(): 
            os.remove(temp_path)
        db.close()

@app.post("/upload")
async def upload_media(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...), 
    album_id: str = None,
    current_user: User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    file_id = uuid.uuid4().hex[:8]
    file_extension = Path(file.filename).suffix.lower()
    if not file_extension: 
        file_extension = ".png" # Дефолт для файлов из буфера обмена (Ctrl+V) без расширения
        
    temp_path = UPLOAD_DIR / f"temp_{file_id}{file_extension}"

    sha256_hash = hashlib.sha256()
    
    with open(temp_path, "wb") as buffer:
        # Читаем и сохраняем файл мощными чанками по 1 МБ
        while chunk := await file.read(1024 * 1024):
            buffer.write(chunk)
            sha256_hash.update(chunk)
            
    file_hash = sha256_hash.hexdigest()

    existing_file = db.query(Media).filter(Media.file_hash == file_hash).first()
    
    is_video = file.content_type.startswith("video/")
    
    if existing_file and existing_file.file_path:
        final_path = Path(existing_file.file_path)
        duration = existing_file.duration
        status = existing_file.status
        os.remove(temp_path)
    else:
        final_filename = f"{file_id}{file_extension}" if not is_video else f"{file_id}.mp4"
        final_path = UPLOAD_DIR / final_filename
        if is_video:
            status = "processing"
            duration = None
            background_tasks.add_task(process_video_background, file_id, temp_path, final_path)
        else:
            shutil.move(str(temp_path), str(final_path))
            status = "ready"
            duration = None

    now = datetime.now(timezone.utc)
    if album_id:
        album = db.query(Album).filter(Album.id == album_id).first()
        expires_at = album.expires_at if album else now + timedelta(days=14)
    else:
        expires_at = now + timedelta(days=14)

    new_media = Media(
        id=file_id, user_id=current_user.id, album_id=album_id,
        filename=file.filename if not is_video else f"{Path(file.filename).stem}.mp4",
        content_type=file.content_type if not is_video else "video/mp4",
        file_hash=file_hash, file_path=str(final_path.resolve()), 
        duration=duration, expires_at=expires_at, status=status
    )
    db.add(new_media)
    db.commit()
    return {"status": "success", "file_id": file_id, "url": f"{BASE_URL}/v/{file_id}"}

@app.post("/upload/album")
async def upload_album(
    files: list[UploadFile] = File(...),
    album_id: str = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not files:
        raise HTTPException(status_code=400, detail="Файлы не выбраны")
        
    for file in files:
        if not file.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="В альбомы можно грузить только фотографии!")

    now = datetime.now(timezone.utc)
    if album_id:
        db_album = db.query(Album).filter(Album.id == album_id).first()
        if not db_album: 
            raise HTTPException(status_code=404, detail="Альбом не найден")
        expires_at = db_album.expires_at
    else:
        album_id = uuid.uuid4().hex[:8]
        expires_at = now + timedelta(days=14)
        db_album = Album(id=album_id, user_id=current_user.id, name="Автоматический альбом", expires_at=expires_at)
        db.add(db_album)

    for file in files:
        media_id = uuid.uuid4().hex[:8]
        file_extension = Path(file.filename).suffix.lower() or ".png"
        final_filename = f"{media_id}{file_extension}"
        final_path = UPLOAD_DIR / final_filename

        with open(final_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        file_hash = calculate_sha256(final_path)
        existing_file = db.query(Media).filter(Media.file_hash == file_hash).first()

        if existing_file and existing_file.file_path:
            os.remove(final_path)
            final_path = Path(existing_file.file_path)

        new_photo = Media(
            id=media_id, user_id=current_user.id, album_id=album_id,
            filename=file.filename, content_type=file.content_type,
            file_hash=file_hash, file_path=str(final_path.resolve()),
            status="ready", expires_at=expires_at
        )
        db.add(new_photo)

    db.commit()
    return {"status": "success", "album_id": album_id, "url": f"{BASE_URL}/a/{album_id}"}

@app.post("/api/albums/create")
def create_empty_album(name: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    album_id = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=14)
    db_album = Album(id=album_id, user_id=current_user.id, name=name, expires_at=expires_at)
    db.add(db_album)
    db.commit()
    return {"status": "success", "album_id": album_id, "name": name}

@app.get("/media/{file_id}")
async def get_media_file(file_id: str, db: Session = Depends(get_db)):
    media = db.query(Media).filter(Media.id == file_id).first()
    if not media or not media.file_path: 
        raise HTTPException(status_code=404, detail="Файл не найден на сервере")
    if media.status != "ready": 
        raise HTTPException(status_code=400, detail="Файл всё еще сжимается")
    return FileResponse(str(Path(media.file_path).resolve()), media_type=media.content_type, headers={"Accept-Ranges": "bytes"})

@app.get("/v/{file_id}", response_class=HTMLResponse)
async def view_media(request: Request, file_id: str, db: Session = Depends(get_db)):
    media = db.query(Media).filter(Media.id == file_id).first()
    if not media: 
        raise HTTPException(status_code=404, detail="Медиафайл удален или никогда не существовал")
    return templates.TemplateResponse(request=request, name="player.html", context={"request": request, "media": media})

@app.get("/a/{album_id}", response_class=HTMLResponse)
async def view_album(request: Request, album_id: str, db: Session = Depends(get_db)):
    album = db.query(Album).filter(Album.id == album_id).first()
    if not album:
        raise HTTPException(status_code=404, detail="Альбом не найден или был удален модерацией")
    return templates.TemplateResponse(
        request=request, name="album.html", 
        context={"request": request, "photos": album.media, "expires_at": album.expires_at.strftime("%Y-%m-%d %H:%M")}
    )

@app.get("/auth/logout")
async def logout():
    """Удаляет куку с токеном и разлогинивает пользователя"""
    response = RedirectResponse(url="/")
    # Удаляем именно ту куку, которую создали при логине (chetmedia_session)
    response.delete_cookie(
        key="chetmedia_session",
        httponly=True,
        samesite="lax"
    )
    return response

@app.get("/api/my-media")
def get_my_media(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    media_files = db.query(Media).filter(Media.user_id == current_user.id, Media.album_id == None).order_by(Media.created_at.desc()).all()
    albums = db.query(Album).filter(Album.user_id == current_user.id).order_by(Album.created_at.desc()).all()
    result = []
    for a in albums:
        result.append({
            "type": "album", "id": a.id, "name": getattr(a, "name", "Без названия"),
            "expires_at": a.expires_at.strftime("%Y-%m-%d %H:%M"), "url": f"{BASE_URL}/a/{a.id}",
            "file_count": db.query(Media).filter(Media.album_id == a.id).count()
        })
    for m in media_files:
        result.append({
            "type": "file", "id": m.id, "filename": m.filename, "content_type": m.content_type,
            "status": m.status, "duration": m.duration, "expires_at": m.expires_at.strftime("%Y-%m-%d %H:%M"),
            "url": f"{BASE_URL}/v/{m.id}"
        })
    return result

# =========================================================================
# АДМИНИСТРАТИВНЫЕ МАРШРУТЫ УПРАВЛЕНИЯ (БАН / РАЗБАН / МОДЕРАЦИЯ МЕДИАТЕКИ)
# =========================================================================
@app.get("/api/admin/users")
async def get_all_users(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Доступ только для администраторов.")
    users = db.query(User).all()
    result = []
    for u in users:
        result.append({
            "id": u.id, "username": u.username, "is_admin": u.is_admin,
            "is_blocked": getattr(u, "is_blocked", False)
        })
    return {"users": result}

@app.post("/api/admin/users/{target_id}/block")
async def block_user(target_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user.is_admin: raise HTTPException(status_code=403, detail="Нет прав")
    target_user = db.query(User).filter(User.id == target_id).first()
    if not target_user: raise HTTPException(status_code=404, detail="Пользователь не найден")
    if target_user.id == current_user.id: raise HTTPException(status_code=400, detail="Нельзя забанить самого себя!")
    
    target_user.is_blocked = True
    user_media = db.query(Media).filter(Media.user_id == target_id).all()
    for m in user_media:
        if m.file_path and Path(m.file_path).exists(): os.remove(m.file_path)
        db.delete(m)
    user_albums = db.query(Album).filter(Album.user_id == target_id).all()
    for a in user_albums: db.delete(a)
    db.commit()
    return {"status": "success"}

@app.post("/api/admin/users/{target_id}/unblock")
async def unblock_user(target_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user.is_admin: raise HTTPException(status_code=403, detail="Нет прав")
    target_user = db.query(User).filter(User.id == target_id).first()
    if not target_user: raise HTTPException(status_code=404, detail="Пользователь не найден")
    target_user.is_blocked = False
    db.commit()
    return {"status": "success"}

@app.get("/api/admin/users/{target_id}/media")
async def get_user_media_admin(target_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user.is_admin: raise HTTPException(status_code=403, detail="Доступ запрещен")
    media_files = db.query(Media).filter(Media.user_id == target_id).order_by(Media.created_at.desc()).all()
    result = []
    for m in media_files:
        result.append({
            "id": m.id, "filename": m.filename, "content_type": m.content_type, "status": m.status, "album_id": m.album_id,
            "expires_at": m.expires_at.strftime("%Y-%m-%d %H:%M"),
            "url": f"{BASE_URL}/v/{m.id}" if not m.album_id else f"{BASE_URL}/a/{m.album_id}"
        })
    return result

@app.delete("/api/admin/media/{file_id}")
def delete_file_admin(file_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user.is_admin: raise HTTPException(status_code=403, detail="Доступ запрещен")
    media = db.query(Media).filter(Media.id == file_id).first()
    if not media: raise HTTPException(status_code=404, detail="Файл не найден")
    if media.album_id:
        album = db.query(Album).filter(Album.id == media.album_id).first()
        if album:
            for item in album.media:
                if item.file_path and Path(item.file_path).exists(): os.remove(item.file_path)
            db.delete(album)
            db.commit()
            return {"status": "album_deleted"}
    if media.file_path and Path(media.file_path).exists(): os.remove(media.file_path)
    db.delete(media)
    db.commit()
    return {"status": "deleted"}

@app.delete("/api/media/{file_id}")
def delete_my_file(file_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Точечное удаление ОДНОГО медиафайла (из корня или изнутри альбома)"""
    media = db.query(Media).filter(Media.id == file_id, Media.user_id == current_user.id).first()
    if not media: 
        raise HTTPException(status_code=404, detail="Файл не найден")
    
    # Теперь мы НЕ удаляем альбом каскадом, если удаляется фотка из него!
    if media.file_path and Path(media.file_path).exists():
        # Броня дедупликации: физически стираем файл только если это последняя ссылка в БД
        count = db.query(Media).filter(Media.file_path == media.file_path).count()
        if count == 1:
            os.remove(media.file_path)
            
    db.delete(media)
    db.commit()
    return {"status": "deleted"}

@app.delete("/api/albums/{album_id}")
def delete_my_album(album_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Полный принудительный снос ВСЕГО альбома и всех его фоток"""
    album = db.query(Album).filter(Album.id == album_id, Album.user_id == current_user.id).first()
    if not album: 
        raise HTTPException(status_code=404, detail="Альбом не найден")
    
    # Физически вычищаем файлы всех вложенных фотографий
    for m in album.media:
        count = db.query(Media).filter(Media.file_path == m.file_path).count()
        if count == 1 and m.file_path and Path(m.file_path).exists():
            os.remove(m.file_path)
            
    db.delete(album)
    db.commit()
    return {"status": "album_deleted"}

@app.get("/api/albums/{album_id}/media")
def get_album_media(album_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Получение файлов исключительно изнутри конкретного альбома"""
    album = db.query(Album).filter(Album.id == album_id, Album.user_id == current_user.id).first()
    if not album: 
        raise HTTPException(status_code=404, detail="Альбом не найден")
    
    result = []
    for m in album.media:
        result.append({
            "type": "file",
            "id": m.id,
            "filename": m.filename,
            "content_type": m.content_type,
            "status": m.status,
            "duration": m.duration,
            "expires_at": m.expires_at.strftime("%Y-%m-%d %H:%M"),
            "url": f"{BASE_URL}/v/{m.id}"
        })
    return result

@app.delete("/api/albums/{album_id}")
def delete_my_album(album_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Полный принудительный снос ВСЕГО альбома и всех его фоток"""
    album = db.query(Album).filter(Album.id == album_id, Album.user_id == current_user.id).first()
    if not album: 
        raise HTTPException(status_code=404, detail="Альбом не найден")
    
    # Физически вычищаем файлы всех вложенных фотографий с диска
    for m in album.media:
        count = db.query(Media).filter(Media.file_path == m.file_path).count()
        if count == 1 and m.file_path and Path(m.file_path).exists():
            os.remove(m.file_path)
            
    db.delete(album)
    db.commit()
    return {"status": "album_deleted"}

@app.get("/api/albums/{album_id}/media")
def get_album_media(album_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Получение файлов исключительно изнутри конкретного альбома"""
    album = db.query(Album).filter(Album.id == album_id, Album.user_id == current_user.id).first()
    if not album: 
        raise HTTPException(status_code=404, detail="Альбом не найден")
    
    result = []
    for m in album.media:
        result.append({
            "type": "file",
            "id": m.id,
            "filename": m.filename,
            "content_type": m.content_type,
            "status": m.status,
            "duration": m.duration,
            "expires_at": m.expires_at.strftime("%Y-%m-%d %H:%M"),
            "url": f"{BASE_URL}/v/{m.id}"
        })
    return result

# Модель для принятия текста жалобы
class ReportRequest(BaseModel):
    reason: str

@app.post("/api/report/{item_id}")
async def report_content(item_id: str, payload: ReportRequest, db: Session = Depends(get_db)):
    """Принимает жалобу и отправляет красивый Embed в Discord-ветку с пингом роли"""
    
    # 1. Ищем контент (это может быть одиночный файл или целый альбом)
    media = db.query(Media).filter(Media.id == item_id).first()
    album = None
    if not media:
        album = db.query(Album).filter(Album.id == item_id).first()
        if not album:
            raise HTTPException(status_code=404, detail="Контент не найден")

    item = media if media else album
    
    # 2. Ищем владельца
    owner = db.query(User).filter(User.id == item.user_id).first()
    owner_text = f"{owner.username} ({owner.id})" if owner else f"Неизвестно ({item.user_id})"

    # 3. Формируем красивый Embed
    content_type = "🎬 Медиафайл" if media else "📁 Альбом"
    content_url = f"{BASE_URL}/v/{item.id}" if media else f"{BASE_URL}/a/{item.id}"

    embed = {
        "title": "Жалоба на контент",
        "description": f"**Причина:** {payload.reason}",
        "color": 15158332, # Красный цвет
        "fields": [
            {"name": "Владелец", "value": owner_text, "inline": True},
            {"name": "Ссылка", "value": f"[{content_type}]({content_url})", "inline": True}
        ],
        "footer": {"text": f"ID: {item.id}"}
    }

    # 4. Отправляем в Discord
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    channel_id = "1507551756492476426" # ID твоей ветки
    
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json"
    }
    
    # ДОБАВЛЕН ПИНГ РОЛИ В content
    discord_payload = {
        "content": "<@&1505359848433516734>",
        "embeds": [embed]
    }
    
    async with httpx.AsyncClient() as client:
        res = await client.post(url, headers=headers, json=discord_payload)
        if res.status_code not in (200, 201):
            print(f"❌ Ошибка отправки жалобы в Discord: {res.text}")
            
    return {"status": "ok"}

@app.post("/api/bulk/delete")
def bulk_delete_media(payload: BulkDeleteRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Массовое удаление файлов и альбомов"""
    for item_id in payload.ids:
        # Ищем среди одиночных файлов
        media = db.query(Media).filter(Media.id == item_id, Media.user_id == current_user.id).first()
        if media:
            if media.file_path and Path(media.file_path).exists():
                count = db.query(Media).filter(Media.file_path == media.file_path).count()
                if count == 1:
                    os.remove(media.file_path)
            db.delete(media)
        else:
            # Если это не файл, ищем среди альбомов
            album = db.query(Album).filter(Album.id == item_id, Album.user_id == current_user.id).first()
            if album:
                for m in album.media:
                    count = db.query(Media).filter(Media.file_path == m.file_path).count()
                    if count == 1 and m.file_path and Path(m.file_path).exists():
                        os.remove(m.file_path)
                db.delete(album)
    db.commit()
    return {"status": "success"}

@app.post("/api/bulk/create-album")
def bulk_create_album(payload: BulkAlbumRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Создает альбом и переносит в него выбранные файлы"""
    media_items = db.query(Media).filter(Media.id.in_(payload.media_ids), Media.user_id == current_user.id).all()
    if not media_items:
        raise HTTPException(status_code=400, detail="Нет доступных файлов для создания альбома")

    album_id = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=14)
    
    # 1. Создаем сам альбом
    db_album = Album(id=album_id, user_id=current_user.id, name=payload.name, expires_at=expires_at)
    db.add(db_album)
    
    # 2. Переписываем всем файлам их новый дом (album_id)
    for m in media_items:
        m.album_id = album_id
        
    db.commit()
    return {"status": "success", "album_id": album_id}

@app.post("/api/bulk/add-to-album")
def bulk_add_to_album(payload: BulkAddToAlbumRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Добавляет (или переносит) выбранные файлы в существующий альбом"""
    # 1. Проверяем, существует ли альбом и принадлежит ли он юзеру
    album = db.query(Album).filter(Album.id == payload.album_id, Album.user_id == current_user.id).first()
    if not album:
        raise HTTPException(status_code=404, detail="Альбом не найден")

    # 2. Ищем выбранные медиафайлы
    media_items = db.query(Media).filter(Media.id.in_(payload.media_ids), Media.user_id == current_user.id).all()
    if not media_items:
        raise HTTPException(status_code=400, detail="Нет доступных файлов для переноса")

    # 3. Переназначаем им альбом
    for m in media_items:
        m.album_id = album.id
        
    db.commit()
    return {"status": "success"}