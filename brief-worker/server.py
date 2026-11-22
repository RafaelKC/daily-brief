"""brief-worker: recebe os dados do n8n, gera o roteiro com Claude Code,
sintetiza o áudio (edge-tts, Piper como reserva) e publica um feed de podcast privado."""
import asyncio
import base64
import hmac
import json
import os
import random
import re
import shutil
import tempfile
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import edge_tts
from PIL import Image, ImageDraw, ImageFont
import aiohttp
from aiohttp import web

DATA = Path(os.environ.get("DATA_DIR", "/data"))
EP_DIR = DATA / "episodes"
DB = DATA / "episodes.json"
APP_DIR = Path(__file__).parent
WORKDIR = Path.home() / "work"

WORKER_TOKEN = os.environ["WORKER_TOKEN"]
FEED_TOKEN = os.environ["FEED_TOKEN"]
BASE = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8093").rstrip("/")
MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
VOICE = os.environ.get("TTS_VOICE", "pt-BR-AntonioNeural")
RATE = os.environ.get("TTS_RATE", "+8%")
KEEP = int(os.environ.get("KEEP_EPISODES", "10"))
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "900"))
PIPER_MODEL = "/opt/piper/pt_BR-faber-medium.onnx"
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts")
GEMINI_VOICE = os.environ.get("GEMINI_TTS_VOICE", "Charon")
GEMINI_STYLE = os.environ.get(
    "GEMINI_TTS_STYLE",
    "Leia o texto a seguir em português do Brasil como o apresentador de um podcast matinal: "
    "tom caloroso e descontraído, ritmo natural de conversa, sem pressa, sorrindo na voz.")
GEMINI_CHUNK = int(os.environ.get("GEMINI_TTS_CHUNK_CHARS", "4500"))

WEEKDAYS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
            "sexta-feira", "sábado", "domingo"]

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "script": {"type": "string"},
        "notes_html": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["title", "script", "notes_html", "summary"],
    "additionalProperties": False,
}

lock = asyncio.Lock()


# ---------- estado ----------
def load_db() -> list[dict]:
    if DB.exists():
        return json.loads(DB.read_text())
    return []


def save_db(eps: list[dict]) -> None:
    tmp = DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(eps, ensure_ascii=False, indent=1))
    tmp.replace(DB)


def authorized(req: web.Request) -> bool:
    got = req.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return hmac.compare_digest(got, WORKER_TOKEN)


# ---------- Claude ----------
async def run_claude(prompt: str) -> dict:
    WORKDIR.mkdir(exist_ok=True)
    cmd = [
        "claude", "-p",
        "--model", MODEL,
        "--output-format", "json",
        "--json-schema", json.dumps(SCHEMA),
        "--allowedTools", "WebFetch", "WebSearch",
        "--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit",
        "--max-turns", "40",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=WORKDIR,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), CLAUDE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"claude timeout ({CLAUDE_TIMEOUT}s)")
    if proc.returncode != 0:
        raise RuntimeError(f"claude exit {proc.returncode}: {err.decode()[-2000:]} {out.decode()[-2000:]}")
    res = json.loads(out)
    if res.get("is_error"):
        raise RuntimeError(f"claude error: {str(res)[:2000]}")
    so = res.get("structured_output")
    if not so:
        m = re.search(r"\{.*\}", res.get("result", ""), re.S)
        so = json.loads(m.group(0)) if m else None
    if not so or not so.get("script"):
        raise RuntimeError(f"saída sem roteiro: {str(res)[:2000]}")
    so["_usage"] = {k: res.get(k) for k in ("total_cost_usd", "num_turns", "duration_ms", "usage")}
    return so


# ---------- TTS ----------
async def tts_edge(text: str, mp3: Path) -> None:
    await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(mp3))


async def run(*cmd: str, stdin: bytes | None = None) -> bytes:
    p = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE if stdin else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate(stdin)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} falhou: {err.decode()[-1000:]}")
    return out


async def tts_piper(text: str, mp3: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "out.wav"
        await run("python", "-m", "piper", "-m", PIPER_MODEL, "-f", str(wav), stdin=text.encode())
        await run("ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
                  "-ac", "1", "-b:a", "64k", str(mp3))


def split_chunks(text: str, limit: int) -> list[str]:
    """Quebra o roteiro em blocos de até `limit` caracteres, respeitando parágrafos e frases."""
    pieces: list[str] = []
    for para in [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]:
        if len(para) <= limit:
            pieces.append(para)
        else:
            pieces.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", para) if s.strip())
    chunks, cur = [], ""
    for p in pieces:
        if cur and len(cur) + len(p) + 2 > limit:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


async def tts_gemini(text: str, mp3: Path) -> None:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"x-goog-api-key": GEMINI_KEY}
    rate = 24000
    pcm = bytearray()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
        for i, chunk in enumerate(split_chunks(text, GEMINI_CHUNK)):
            body = {
                "contents": [{"parts": [{"text": f"{GEMINI_STYLE}\n\n{chunk}"}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": GEMINI_VOICE}}},
                },
            }
            for attempt in range(3):
                async with s.post(url, json=body, headers=headers) as r:
                    js = await r.json(content_type=None)
                if r.status == 200:
                    break
                msg = str(js.get("error", js))[:300]
                if r.status == 429 or attempt == 2:  # cota esgotada não adianta repetir
                    raise RuntimeError(f"gemini {r.status}: {msg}")
                await asyncio.sleep(5 * (attempt + 1))
            part = js["candidates"][0]["content"]["parts"][0]["inlineData"]
            m = re.search(r"rate=(\d+)", part.get("mimeType", ""))
            rate = int(m.group(1)) if m else rate
            if i:
                pcm += b"\x00\x00" * int(rate * 0.35)  # respiro entre blocos
            pcm += base64.b64decode(part["data"])
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "out.pcm"
        raw.write_bytes(pcm)
        await run("ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1",
                  "-i", str(raw), "-b:a", "64k", str(mp3))


async def synth(text: str, mp3: Path) -> str:
    if GEMINI_KEY:
        try:
            await tts_gemini(text, mp3)
            if mp3.stat().st_size > 10_000:
                return f"gemini:{GEMINI_MODEL}:{GEMINI_VOICE}"
        except Exception as e:  # noqa: BLE001
            print(f"gemini-tts falhou, usando edge-tts: {e}", flush=True)
    try:
        await tts_edge(text, mp3)
        if mp3.stat().st_size > 10_000:
            return "edge-tts"
    except Exception as e:  # noqa: BLE001
        print(f"edge-tts falhou, usando piper: {e}", flush=True)
    await tts_piper(text, mp3)
    return "piper"


async def duration(mp3: Path) -> int:
    out = await run("ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=nw=1:nk=1", str(mp3))
    return int(float(out.decode().strip() or 0))


# ---------- capas (identidade visual rndm / chicovis.dev) ----------
FONT_DIR = Path(os.environ.get("FONT_DIR", "/opt/fonts"))
ARTWORK_BRAND = os.environ.get("ARTWORK_BRAND", "")
ARTWORK_VERSION = "2"  # mude para forçar os apps a baixarem as capas de novo
BG, ACCENT, WHITE, DIM = (10, 10, 10), (255, 107, 0), (248, 250, 252), (203, 213, 225)
ART = 1400


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / name), size)


def _spaced(d: ImageDraw.ImageDraw, x: int, y: int, text: str, font, fill, spacing: int) -> int:
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        x += int(d.textlength(ch, font=font)) + spacing
    return x


def _wave(d: ImageDraw.ImageDraw, x: int, y_mid: int, seed: str, n: int = 20, w: int = 22, gap: int = 18, hmax: int = 250) -> None:
    rnd = random.Random(seed)
    for _ in range(n):
        h = rnd.randint(int(hmax * 0.18), hmax)
        d.rounded_rectangle([x, y_mid - h // 2, x + w, y_mid + h // 2], radius=w // 2, fill=ACCENT)
        x += w + gap


def render_artwork(path: Path, date: str | None = None, test: bool = False) -> None:
    """Capa quadrada 1400x1400: a do podcast (date=None) ou a de um episódio, com a data."""
    im = Image.new("RGB", (ART, ART), BG)
    d = ImageDraw.Draw(im)
    m = 110
    d.rectangle([0, 0, ART, 14], fill=ACCENT)
    if date is None:
        _spaced(d, m, 150, "06:30 · DIAS ÚTEIS", _font("Inter-Bold.ttf", 56), ACCENT, 6)
        title = _font("Inter-ExtraBold.ttf", 250)
        d.text((m, 250), "Brief", font=title, fill=WHITE)
        d.text((m, 520), "Matinal", font=title, fill=WHITE)
        d.text((m, 850), "Seu dia em menos de 10 minutos", font=_font("Inter-Regular.ttf", 60), fill=DIM)
        _wave(d, m, 1080, "brief-matinal")
    else:
        dt = datetime.fromisoformat(date)
        _spaced(d, m, 150, "BRIEF MATINAL", _font("Inter-Bold.ttf", 56), ACCENT, 8)
        d.text((m, 250), WEEKDAYS[dt.weekday()].capitalize(), font=_font("Inter-Regular.ttf", 90), fill=DIM)
        d.text((m, 360), dt.strftime("%d/%m"), font=_font("FiraCode-Bold.ttf", 330), fill=WHITE)
        yfont = _font("FiraCode-Regular.ttf", 96)
        d.text((m + 8, 770), str(dt.year), font=yfont, fill=ACCENT)
        if test:
            x0 = m + 8 + int(d.textlength(str(dt.year), font=yfont)) + 50
            pf = _font("Inter-Bold.ttf", 48)
            tw = int(d.textlength("TESTE", font=pf))
            d.rounded_rectangle([x0, 785, x0 + tw + 60, 875], radius=45, outline=ACCENT, width=4)
            d.text((x0 + 30, 830), "TESTE", font=pf, fill=ACCENT, anchor="lm")
        _wave(d, m, 1080, date)
    if ARTWORK_BRAND:
        d.text((ART - m, ART - 90), ARTWORK_BRAND, font=_font("Inter-ExtraBold.ttf", 56), fill=ACCENT, anchor="rd")
    im.save(path, optimize=True)


def ensure_artwork(eps: list[dict]) -> bool:
    """Gera a capa do podcast e a de cada episódio que ainda não tem. Devolve True se mudou algo."""
    changed = False
    marker = DATA / f"cover-v{ARTWORK_VERSION}.ok"
    if not marker.exists() or not (DATA / "cover.png").exists():
        render_artwork(DATA / "cover.png")
        marker.write_text("ok")
        for e in eps:
            e.pop("image", None)
    for e in eps:
        img = e.get("image")
        if not img or not (EP_DIR / img).exists():
            img = e["file"].replace(".mp3", ".png")
            render_artwork(EP_DIR / img, e["date"], bool(e.get("test")))
            e["image"] = img
            changed = True
    return changed


# ---------- feed ----------
def feed_url(path: str) -> str:
    return f"{BASE}/{FEED_TOKEN}/{path}"


def build_feed(eps: list[dict]) -> str:
    items = []
    for e in sorted(eps, key=lambda x: x["created_at"], reverse=True):
        pub = format_datetime(datetime.fromisoformat(e["created_at"]))
        d = e.get("duration", 0)
        img = (f'\n    <itunes:image href="{feed_url("ep/" + e["image"])}?v={ARTWORK_VERSION}"/>'
               if e.get("image") else "")
        items.append(f"""
  <item>
    <title>{escape(e['title'])}</title>
    <guid isPermaLink="false">brief-{e['date']}-{e['id']}</guid>
    <pubDate>{pub}</pubDate>
    <description><![CDATA[{e['notes_html']}]]></description>
    <content:encoded><![CDATA[{e['notes_html']}]]></content:encoded>
    <itunes:summary>{escape(e.get('summary', ''))}</itunes:summary>
    <enclosure url="{feed_url('ep/' + e['file'])}" length="{e['size']}" type="audio/mpeg"/>{img}
    <itunes:duration>{d // 60}:{d % 60:02d}</itunes:duration>
    <itunes:explicit>false</itunes:explicit>
  </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
  <title>Brief Matinal</title>
  <link>{feed_url('feed.xml')}</link>
  <language>pt-br</language>
  <description>Brief diário privado: agenda, tarefas, pagamentos, e-mails e notícias.</description>
  <itunes:author>Brief Matinal</itunes:author>
  <itunes:block>Yes</itunes:block>
  <itunes:explicit>false</itunes:explicit>
  <itunes:image href="{feed_url('cover.png')}?v={ARTWORK_VERSION}"/>
  <image><url>{feed_url('cover.png')}?v={ARTWORK_VERSION}</url><title>Brief Matinal</title><link>{feed_url('feed.xml')}</link></image>
  <itunes:category text="News"/>{''.join(items)}
</channel>
</rss>
"""


async def ensure_cover() -> None:
    if not (DATA / "cover.png").exists():
        render_artwork(DATA / "cover.png")


# ---------- handlers ----------
async def h_state(req: web.Request) -> web.Response:
    if not authorized(req):
        return web.json_response({"error": "unauthorized"}, status=401)
    eps = load_db()
    real = [e for e in eps if not e.get("test")]
    last = max(real, key=lambda x: x["created_at"]) if real else None
    return web.json_response({
        "last_run": last["created_at"] if last else None,
        "last_title": last["title"] if last else None,
        "episodes": len(eps),
        "feed_url": feed_url("feed.xml"),
    })


async def h_brief(req: web.Request) -> web.Response:
    if not authorized(req):
        return web.json_response({"error": "unauthorized"}, status=401)
    body = await req.json()
    dry = bool(body.get("dry_run"))
    now = datetime.now().astimezone()
    date = body.get("date") or now.date().isoformat()
    d = datetime.fromisoformat(date)

    async with lock:
        eps = load_db()
        real = [e for e in eps if not e.get("test")]
        last = max(real, key=lambda x: x["created_at"]) if real else None
        cutoff = body.get("cutoff") or (last["created_at"] if last else "últimas 24 horas")
        pfile = APP_DIR / "prompt.md"
        if not pfile.exists():
            pfile = APP_DIR / "prompt.example.md"
        prompt = pfile.read_text()
        prompt = (prompt.replace("{{OWNER}}", os.environ.get("BRIEF_OWNER", "usuário"))
                  .replace("{{CITY}}", os.environ.get("BRIEF_CITY", "sua cidade")))
        prompt = (prompt.replace("{{DATE}}", d.strftime("%d/%m/%Y"))
                  .replace("{{WEEKDAY}}", WEEKDAYS[d.weekday()])
                  .replace("{{CUTOFF}}", str(cutoff))
                  .replace("{{PREVIOUS_NOTES}}", (last or {}).get("notes_html", "(nenhuma)"))
                  .replace("{{DATA_JSON}}", json.dumps(body.get("data", {}), ensure_ascii=False)))

        out = await run_claude(prompt)
        words = len(out["script"].split())
        if dry:
            return web.json_response({"ok": True, "dry_run": True, "words": words, **out})

        EP_DIR.mkdir(parents=True, exist_ok=True)
        ep_id = now.strftime("%H%M%S")
        fname = f"{date}-{ep_id}.mp3"
        mp3 = EP_DIR / fname
        engine = await synth(out["script"], mp3)
        dur = await duration(mp3)

        ep = {"id": ep_id, "date": date, "created_at": now.isoformat(timespec="seconds"),
              "title": out["title"], "summary": out["summary"], "notes_html": out["notes_html"],
              "script": out["script"], "file": fname, "size": mp3.stat().st_size,
              "duration": dur, "tts": engine, "test": bool(body.get("test"))}
        if ep["test"]:
            ep["title"] = "[Teste] " + ep["title"]
        ep["image"] = fname.replace(".mp3", ".png")
        render_artwork(EP_DIR / ep["image"], date, ep["test"])
        eps.append(ep)
        eps.sort(key=lambda x: x["created_at"])
        for old in eps[:-KEEP]:
            (EP_DIR / old["file"]).unlink(missing_ok=True)
            if old.get("image"):
                (EP_DIR / old["image"]).unlink(missing_ok=True)
        eps = eps[-KEEP:]
        save_db(eps)
        await ensure_cover()
        (DATA / "feed.xml").write_text(build_feed(eps))

    return web.json_response({"ok": True, "title": ep["title"], "summary": ep["summary"],
                              "words": words, "duration_s": dur, "tts": engine,
                              "episode_url": feed_url("ep/" + fname),
                              "feed_url": feed_url("feed.xml"), "usage": out["_usage"]})


def feed_guard(req: web.Request) -> None:
    if not hmac.compare_digest(req.match_info["tok"], FEED_TOKEN):
        raise web.HTTPNotFound()


async def h_feed(req: web.Request) -> web.StreamResponse:
    feed_guard(req)
    f = DATA / "feed.xml"
    if not f.exists():
        f.write_text(build_feed(load_db()))
    return web.Response(body=f.read_bytes(), content_type="application/rss+xml", charset="utf-8",
                        headers={"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-cache"})


async def h_cover(req: web.Request) -> web.StreamResponse:
    feed_guard(req)
    await ensure_cover()
    return web.FileResponse(DATA / "cover.png")


async def h_ep(req: web.Request) -> web.StreamResponse:
    feed_guard(req)
    name = req.match_info["name"]
    m = re.fullmatch(r"[\w.-]+\.(mp3|png)", name)
    if not m:
        raise web.HTTPNotFound()
    p = EP_DIR / name
    if not p.exists():
        raise web.HTTPNotFound()
    ctype = "audio/mpeg" if m.group(1) == "mp3" else "image/png"
    return web.FileResponse(p, headers={"Content-Type": ctype, "X-Robots-Tag": "noindex"})


async def h_health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "claude": bool(shutil.which("claude"))})


async def h_robots(_: web.Request) -> web.Response:
    return web.Response(text="User-agent: *\nDisallow: /\n")


PAGE = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{title}</title><style>body{{font:16px/1.6 system-ui,sans-serif;max-width:680px;margin:40px auto;padding:0 16px;color:#222}}</style>
</head><body>{body}</body></html>"""

HOME_HTML = PAGE.format(title="Brief Matinal", body="""<h1>Brief Matinal</h1>
<p>Automação pessoal e privada que gera, em dias úteis, um resumo em áudio (podcast) com a agenda, as tarefas,
os pagamentos, os e-mails e as notícias do dia do seu único usuário, o proprietário deste domínio.</p>
<p>Não é um serviço público e não aceita cadastro de terceiros. <a href="/privacidade">Política de Privacidade</a>.</p>""")

PRIVACY_HTML = PAGE.format(title="Política de Privacidade", body="""<h1>Política de Privacidade — Brief Matinal</h1>
<p><b>Uso exclusivo do proprietário.</b> O Brief Matinal é uma automação pessoal, usada apenas pelo proprietário deste domínio. Ela não é oferecida a terceiros.</p>
<h2>Dados acessados</h2>
<ul><li><b>Gmail</b> (somente leitura): remetente, assunto, trecho e corpo de e-mails recentes, para montar o resumo diário.</li>
<li><b>Google Calendar</b> (somente leitura): eventos do dia e do próximo dia útil.</li>
<li>Tarefas (TickTick) e dados financeiros pessoais do próprio usuário.</li></ul>
<h2>Uso dos dados</h2>
<p>Os dados são usados apenas para gerar o resumo diário em texto e áudio, entregue somente ao próprio usuário por um feed privado.
Eles não são vendidos, compartilhados nem usados para publicidade. O texto é processado por um modelo de IA (Claude, da Anthropic) apenas para escrever o resumo.</p>
<h2>Armazenamento</h2>
<p>Os dados ficam nos servidores pessoais do proprietário. Ficam guardados só os últimos episódios gerados (áudio e notas); os demais são apagados automaticamente.</p>
<h2>Uso limitado (Google API Services User Data Policy)</h2>
<p>O uso e a transferência de informações recebidas das APIs do Google seguem a Google API Services User Data Policy, incluindo os requisitos de uso limitado.</p>
<h2>Revogação e contato</h2>
<p>O acesso pode ser revogado a qualquer momento em <a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a>.
Contato: o e-mail de suporte informado na tela de consentimento do Google.</p>""")


async def h_home(_: web.Request) -> web.Response:
    return web.Response(text=HOME_HTML, content_type="text/html")


async def h_privacy(_: web.Request) -> web.Response:
    return web.Response(text=PRIVACY_HTML, content_type="text/html")


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    EP_DIR.mkdir(parents=True, exist_ok=True)
    eps = load_db()
    if ensure_artwork(eps):
        save_db(eps)
    (DATA / "feed.xml").write_text(build_feed(eps))
    app = web.Application(client_max_size=50 * 1024 * 1024)
    app.add_routes([
        web.get("/", h_home),
        web.get("/privacidade", h_privacy),
        web.get("/health", h_health),
        web.get("/robots.txt", h_robots),
        web.get("/api/state", h_state),
        web.post("/api/brief", h_brief),
        web.get("/{tok}/feed.xml", h_feed),
        web.get("/{tok}/cover.png", h_cover),
        web.get("/{tok}/ep/{name}", h_ep),
    ])
    web.run_app(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
