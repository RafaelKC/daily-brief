FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    PATH=/home/app/.local/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates tzdata bash \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 app && mkdir -p /data && chown app:app /data
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Piper fallback voice (local TTS, used only if edge-tts fails)
RUN mkdir -p /opt/piper \
 && curl -fsSL -o /opt/piper/pt_BR-faber-medium.onnx \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx \
 && curl -fsSL -o /opt/piper/pt_BR-faber-medium.onnx.json \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx.json

# Fontes da identidade visual (Inter + Fira Code) para as capas
RUN mkdir -p /opt/fonts \
 && curl -fsSL -o /tmp/inter.zip https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip \
 && curl -fsSL -o /tmp/fira.zip https://github.com/tonsky/FiraCode/releases/download/6.2/Fira_Code_v6.2.zip \
 && python -c "import zipfile; z=zipfile.ZipFile('/tmp/inter.zip'); [open('/opt/fonts/'+n.split('/')[-1],'wb').write(z.read(n)) for n in z.namelist() if n.split('/')[-1] in ('Inter-Regular.ttf','Inter-Bold.ttf','Inter-ExtraBold.ttf')]; z=zipfile.ZipFile('/tmp/fira.zip'); [open('/opt/fonts/'+n.split('/')[-1],'wb').write(z.read(n)) for n in z.namelist() if n.split('/')[-1] in ('FiraCode-Regular.ttf','FiraCode-Bold.ttf')]" \
 && rm /tmp/inter.zip /tmp/fira.zip && ls /opt/fonts

USER app
# Claude Code native binary (linux arm64/x64)
RUN curl -fsSL https://claude.ai/install.sh | bash && claude --version

COPY --chown=app:app server.py prompt*.md ./
RUN mkdir -p /home/app/work

EXPOSE 8080
CMD ["python", "server.py"]
