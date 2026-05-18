FROM python:3.11-slim

# Устанавливаем системный FFmpeg и утилиты
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем и устанавливаем зависимости Питона
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной код проекта
COPY . .

# Открываем порт для FastAPI
EXPOSE 8000

# Команда для вечного запуска uvicorn внутри докера
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]