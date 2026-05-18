# Базовый образ с Python
FROM python:3.11-slim

# Устанавливаем FFmpeg и ffprobe внутри контейнера Linux
RUN apt-get update && apt-get install -y ffmpeg && apt-get clean && rm -rf /var/lib/apt/lists/*

# Ставим рабочую папку внутри контейнера
WORKDIR /workspace

# Копируем список зависимостей и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной код проекта
COPY . .

# Открываем порт 8000 наружу
EXPOSE 8000

# Команда для запуска сервера
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]