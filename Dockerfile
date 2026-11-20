FROM python:3.11-slim-bookworm

# Java 17 ve Android APK analiz/imzalama araçlarını kur
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jdk-headless \
    apktool \
    aapt \
    zipalign \
    apksigner \
    && rm -rf /var/lib/apt/lists/*

# Render Free (512 MB RAM) için Java bellek sınırını güvenli düzeyde tut
ENV JAVA_TOOL_OPTIONS="-Xmx256m"
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# APK imzalama için yerel keystore dosyasını derleme sırasında oluştur
RUN keytool -genkeypair -v -keystore /app/retrodroid.jks \
    -keyalg RSA -keysize 2048 -validity 10000 -alias retrodroid \
    -storepass 123456 -keypass 123456 \
    -dname "CN=RetroDroid, OU=Compat, O=Render, C=TR"

COPY . .

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000}"]
