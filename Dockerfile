FROM python:3.12-slim

# WeasyPrint system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 \
    libffi-dev libcairo2 libpangoft2-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Client IP behind Cloud Run: uvicorn keeps --proxy-headers but trusts X-Forwarded-For
# only from the addresses in FORWARDED_ALLOW_IPS (uvicorn's own env var, default
# 127.0.0.1). Do NOT set it to '*': uvicorn then takes the LEFTMOST X-Forwarded-For
# entry, which the visitor controls. The rate limiters instead read the visitor from
# the right-hand end of X-Forwarded-For, set TRUSTED_PROXY_HOPS=1 for Cloud Run
# (see app/security/client_ip.py).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
