FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps --only-shell --no-progress chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app.py .

CMD ["python", "-u", "app.py"]
