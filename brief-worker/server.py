"""brief-worker: recebe os dados do n8n, gera o roteiro com Claude Code,
sintetiza o áudio (edge-tts, Piper como reserva) e publica um feed de podcast privado."""
import asyncio
import hmac
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import edge_tts
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


async def synth(text: str, mp3: Path) -> str:
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


# ---------- feed ----------
def feed_url(path: str) -> str:
    return f"{BASE}/{FEED_TOKEN}/{path}"


def build_feed(eps: list[dict]) -> str:
    items = []
    for e in sorted(eps, key=lambda x: x["created_at"], reverse=True):
        pub = format_datetime(datetime.fromisoformat(e["created_at"]))
        d = e.get("duration", 0)
        items.append(f"""
  <item>
    <title>{escape(e['title'])}</title>
    <guid isPermaLink="false">brief-{e['date']}-{e['id']}</guid>
    <pubDate>{pub}</pubDate>
    <description><![CDATA[{e['notes_html']}]]></description>
    <content:encoded><![CDATA[{e['notes_html']}]]></content:encoded>
    <itunes:summary>{escape(e.get('summary', ''))}</itunes:summary>
    <enclosure url="{feed_url('ep/' + e['file'])}" length="{e['size']}" type="audio/mpeg"/>
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
  <itunes:image href="{feed_url('cover.png')}"/>
  <image><url>{feed_url('cover.png')}</url><title>Brief Matinal</title><link>{feed_url('feed.xml')}</link></image>
  <itunes:category text="News"/>{''.join(items)}
</channel>
</rss>
"""


async def ensure_cover() -> None:
    cover = DATA / "cover.png"
    if not cover.exists():
        await run("ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                  "-i", "color=c=0x1d3557:s=1400x1400", "-frames:v", "1", str(cover))


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
        eps.append(ep)
        eps.sort(key=lambda x: x["created_at"])
        for old in eps[:-KEEP]:
            (EP_DIR / old["file"]).unlink(missing_ok=True)
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
    if not re.fullmatch(r"[\w.-]+\.mp3", name):
        raise web.HTTPNotFound()
    p = EP_DIR / name
    if not p.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(p, headers={"Content-Type": "audio/mpeg", "X-Robots-Tag": "noindex"})


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
