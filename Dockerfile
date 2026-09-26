FROM python:3.11-slim

# Deps para o Playwright e PostgreSQL
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libatspi2.0-0 libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instala navegadores do Playwright
RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .

# Coleta arquivos estáticos (admin)
RUN python manage.py collectstatic --noinput 2>/dev/null || true

EXPOSE 8000

CMD ["uvicorn", "robopod.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--reload"]
