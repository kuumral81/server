FROM python:3.11-slim-bookworm

# Java 17 ve Android APK analiz/paketleme/imzalama araclari
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jdk-headless \
    apktool \
    aapt \
    zipalign \
    apksigner \
    && rm -rf /var/lib/apt/lists/*

# Render Free (512 MB RAM) ortaminda Java bellek tasmasini onle
ENV JAVA_TOOL_OPTIONS="-Xmx256m"
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Sabit bir imzalama anahtari (keystore) olustur
RUN keytool -genkeypair -v -keystore /app/retrodroid.jks \
    -keyalg RSA -keysize 2048 -validity 10000 -alias retrodroid \
    -storepass 123456 -keypass 123456 \
    -dname "CN=RetroDroid, OU=Compat, O=Render, C=TR"

# Kurulan araclari derleme asamasinda dogrula
RUN apktool --version && apksigner --version && aapt version

COPY . .

# Render calisma aninda PORT ortam degiskenini otomatik atar
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000}"]
