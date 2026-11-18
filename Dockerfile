FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/hisob.db
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py index.html ./
CMD ["python", "bot.py"]
