#!/usr/bin/env python3
"""
panel.py - Panel web para revisar los clips generados, elegir cuáles publicar,
editar títulos y descripciones (Gemini las propone por plataforma) y preparar o subir.

Instalación (una vez):
    pip install -U gradio google-genai google-api-python-client google-auth-oauthlib

Los lotes se guardan ordenados: clips/AAAA-MM/AAAA-MM-DD_HHMM_nombre/ (los lotes viejos siguen apareciendo).

Uso en tu compu:
    python panel.py --carpeta clips
Uso en Colab (te da un link público; ponele clave):
    !python -u panel.py --carpeta clips --compartir --clave MiClaveSecreta

Qué hace:
  * Muestra los videos de la carpeta (la que genera auto_clips.py) con su reproductor.
  * Cada clip: casilla "elegir", plataformas, y textos editables para YouTube, TikTok, Instagram, Facebook.
  * Botón "Mejorar con Gemini": escribe título/descripción/hashtags adaptados a cada plataforma.
  * "Preparar paquete": carpeta + ZIP por clip con el video y los textos listos para copiar y pegar.
  * "Subir a YouTube": subida directa (ver advertencia más abajo).

IMPORTANTE sobre publicar automáticamente:
  * YouTube: un proyecto de Google sin auditar sube los videos SIEMPRE como privados.
  * TikTok: los clientes de API sin auditar solo pueden publicar en privado.
  * Instagram y Facebook: la API exige crear una app de Meta con tokens (no incluido en esta versión).
  Por eso el "Paquete" (subida manual con los textos ya escritos) es el camino que funciona hoy para todas.
"""
import argparse
import csv
import inspect
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import redes  # noqa: E402  (publicar por API y buscar música)

PLATAFORMAS = ["YouTube", "TikTok", "Instagram", "Facebook"]
ENLACES = {
    "YouTube": "https://studio.youtube.com",
    "TikTok": "https://www.tiktok.com/tiktokstudio/upload",
    "Instagram": "https://www.instagram.com/",
    "Facebook": "https://business.facebook.com/latest/composer",
}
MODELOS = [os.environ.get("GEMINI_MODEL", "gemini-flash-latest"),
           "gemini-3.5-flash", "gemini-2.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"]

BASE = Path(".")         # carpeta madre: cada video procesado queda en su propia subcarpeta ("lote")
CARPETA = Path(".")      # lote que se está viendo ahora
ESTADO = {}              # archivo -> datos editables de cada clip
GUIA = ""
SCRIPT_AUTO = Path(__file__).resolve().parent / "auto_clips.py"


# ------------------------------------------------------------------ datos
def textos_iniciales(clip):
    titulo = (clip.get("title") or "").strip()
    desc = (clip.get("description") or "").strip()
    tags = (clip.get("hashtags") or "").strip()
    base = f"{desc}\n\n{tags}".strip()
    return {
        "yt_titulo": titulo[:100],
        "yt_desc": (base + " #Shorts").strip(),
        "tt": base,
        "ig": base,
        "fb": desc,
    }


def slug(texto, n=40):
    crudo = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", crudo).strip("-")[:n].strip("-")


def nombre_fuente(fuente):
    """Nombre corto para la carpeta: el archivo sin extensión, o la red de donde viene el link."""
    f = (fuente or "").strip()
    if re.match(r"https?://", f):
        host = f.split("://", 1)[1].split("/")[0].replace("www.", "").split(".")[0]
        return host or "video"
    return Path(f).stem


def ruta_lote(fuente, ahora=None):
    """clips/AAAA-MM/AAAA-MM-DD_HHMM_nombre : agrupado por mes, ordenado por fecha y hora."""
    t = ahora or time.localtime()
    nombre = time.strftime("%Y-%m-%d_%H%M", t)
    s = slug(nombre_fuente(fuente))
    if s:
        nombre += "_" + s
    destino = BASE / time.strftime("%Y-%m", t) / nombre
    n = 2
    while destino.exists():          # dos videos en el mismo minuto: no se pisan
        destino = BASE / time.strftime("%Y-%m", t) / f"{nombre}-{n}"
        n += 1
    return destino


RE_NUEVO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})(?:_(.*))?$")
RE_VIEJO = re.compile(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$")   # formato anterior: 20261026_194500


def describir(ruta):
    """(mes, etiqueta legible) según el nombre de la carpeta del lote."""
    m = RE_NUEVO.match(ruta.name)
    if m:
        y, mo, d, hh, mi, resto = m.groups()
        return f"{y}-{mo}", f"{y}-{mo}-{d} {hh}:{mi}" + (f" · {resto}" if resto else "")
    m = RE_VIEJO.match(ruta.name)
    if m:
        y, mo, d, hh, mi, _ = m.groups()
        return f"{y}-{mo}", f"{y}-{mo}-{d} {hh}:{mi}"
    return "Sin fecha", ruta.name


def lotes_info():
    """Todos los lotes (carpetas con clips.json), el más reciente primero.
    Encuentra el formato nuevo (clips/AAAA-MM/lote), el viejo (clips/lote) y la carpeta principal."""
    rutas = set()
    if (BASE / "clips.json").exists():
        rutas.add(BASE)
    if BASE.exists():
        for patron in ("*/clips.json", "*/*/clips.json"):
            for cj in BASE.glob(patron):
                if not cj.parent.name.startswith("_"):
                    rutas.add(cj.parent)
    info, usadas = [], set()
    for r in sorted(rutas, key=lambda p: (p / "clips.json").stat().st_mtime, reverse=True):
        mes, et = ("Sin fecha", "(carpeta principal)") if r == BASE else describir(r)
        if et in usadas:
            et = f"{et} [{r.name}]"
        usadas.add(et)
        info.append({"mes": mes, "etiqueta": et, "ruta": r})
    return info


def lotes(mes=None):
    """{etiqueta: ruta}, opcionalmente solo los de un mes (AAAA-MM)."""
    return {i["etiqueta"]: i["ruta"] for i in lotes_info() if not mes or mes == "Todos" or i["mes"] == mes}


def meses():
    return ["Todos"] + sorted({i["mes"] for i in lotes_info()}, reverse=True)


def etiqueta_de(ruta):
    ruta = Path(ruta).resolve()
    return next((i["etiqueta"] for i in lotes_info() if i["ruta"] == ruta), None)


def zip_mes(mes):
    """ZIP con los mp4 (y textos) de todos los lotes de un mes. Útil en Codespaces: el disco se borra con el Codespace."""
    if not mes or mes == "Todos":
        return None, "Elegí un mes concreto en el desplegable **Mes** (por ejemplo 2026-10)."
    del_mes = [i for i in lotes_info() if i["mes"] == mes]
    if not del_mes:
        return None, f"No hay lotes en {mes}."
    carpeta_zips = BASE / "_zips"
    carpeta_zips.mkdir(parents=True, exist_ok=True)
    destino = carpeta_zips / f"clips_{mes}.zip"
    n = 0
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_STORED) as z:     # los mp4 ya vienen comprimidos
        for it in del_mes:
            for p in sorted(it["ruta"].glob("*.mp4")):
                z.write(p, f"{it['ruta'].name}/{p.name}")
                n += 1
            for extra in ("clips.json", "publicar.csv", "panel_estado.json"):
                if (it["ruta"] / extra).exists():
                    z.write(it["ruta"] / extra, f"{it['ruta'].name}/{extra}")
    mb = destino.stat().st_size / 1_048_576
    if n == 0:
        destino.unlink()
        return None, f"{mes}: no encontré videos .mp4."
    return str(destino), f"✅ {mes}: **{n}** videos de {len(del_mes)} lote(s), {mb:.0f} MB. Descargalo abajo."


def cargar_guia():
    global GUIA
    guia = Path(__file__).resolve().parent / "guia_contenido.md"
    GUIA = guia.read_text(encoding="utf-8") if guia.exists() else ""


def cargar(carpeta):
    """Carga los clips de un lote (carpeta con clips.json)."""
    global CARPETA, ESTADO
    CARPETA = Path(carpeta).expanduser().resolve()
    cj = CARPETA / "clips.json"
    ESTADO = {}
    if not cj.exists():
        return ESTADO
    clips = json.loads(cj.read_text(encoding="utf-8"))

    archivos = []
    csv_path = CARPETA / "publicar.csv"
    if csv_path.exists():
        with open(csv_path, encoding="utf-8-sig") as f:
            archivos = [r["archivo"] for r in csv.DictReader(f)]
    if not archivos:
        archivos = sorted(p.name for p in CARPETA.glob("*.mp4"))

    guardado = {}
    estado_path = CARPETA / "panel_estado.json"
    if estado_path.exists():
        try:
            guardado = json.loads(estado_path.read_text(encoding="utf-8"))
        except Exception:
            guardado = {}

    for clip, nombre in zip(clips, archivos):
        if not (CARPETA / nombre).exists():
            continue
        it = {"clip": clip, "sel": False, "plat": list(PLATAFORMAS), **textos_iniciales(clip)}
        it.update({k: v for k, v in guardado.get(nombre, {}).items() if k in it and k != "clip"})
        ESTADO[nombre] = it
    cargar_guia()
    return ESTADO


def guardar(nombre, campo, valor):
    if nombre in ESTADO:
        ESTADO[nombre][campo] = valor
    salida = {n: {k: v for k, v in it.items() if k != "clip"} for n, it in ESTADO.items()}
    (CARPETA / "panel_estado.json").write_text(json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ Gemini
REGLAS = """Escribís los textos de publicación de un clip corto de una predicación o mensaje cristiano evangélico.
Respetá la GUÍA DE CONTENIDO. Usá solo lo que se dice en el clip; no inventes citas ni versículos.
Adaptá cada plataforma:
- youtube: "title" (máx. 70 caracteres, claro y que despierte interés sin engañar) y "description" (2 o 3 líneas,
  invitación a suscribirse/compartir y 3 a 5 hashtags, incluyendo #Shorts).
- tiktok: "caption" corto (máx. 150 caracteres) más 3 a 5 hashtags.
- instagram: "caption" con un primer renglón fuerte, 1 o 2 frases, una invitación a comentar o compartir, y 5 a 8 hashtags.
- facebook: "caption" conversacional de 2 o 3 frases con una invitación a compartir, y como mucho 2 hashtags.
Respondé ÚNICAMENTE con este JSON, sin texto antes ni después:
{"youtube":{"title":"","description":""},"tiktok":{"caption":""},"instagram":{"caption":""},"facebook":{"caption":""}}
"""


def gemini_json(system, user):
    from google import genai
    from google.genai import types
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        raise RuntimeError("Falta la variable de entorno GEMINI_API_KEY.")
    client = genai.Client()
    cfg = types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json",
                                      max_output_tokens=4000, temperature=0.6)
    ultimo = ""
    for modelo in dict.fromkeys(MODELOS):
        for intento in range(1, 3):
            try:
                texto = client.models.generate_content(model=modelo, contents=user, config=cfg).text or ""
                a, b = texto.find("{"), texto.rfind("}")
                return json.loads(texto[a:b + 1])
            except Exception as e:
                ultimo = " ".join(str(e).split())[:300]
                temporal = any(k in ultimo for k in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE"))
                if temporal and intento < 2 and "limit: 0" not in ultimo:
                    time.sleep(20)
                    continue
                break
    raise RuntimeError(f"Gemini no respondió: {ultimo}")


def generar_textos(nombre, extra=""):
    it = ESTADO[nombre]
    clip = it["clip"]
    user = (
        f"Gancho del clip: {clip.get('hook', '')}\n"
        f"Título actual: {it['yt_titulo']}\n"
        f"Texto hablado en el clip: {(clip.get('texto') or '(no disponible)')[:2000]}\n"
        f"Descripción actual: {clip.get('description', '')}\n"
    )
    if extra and extra.strip():
        user += f"Indicaciones extra del usuario: {extra.strip()}\n"
    r = gemini_json(REGLAS + "\n=== GUÍA DE CONTENIDO ===\n" + GUIA, user)
    yt, tt, ig, fb = r.get("youtube", {}), r.get("tiktok", {}), r.get("instagram", {}), r.get("facebook", {})
    nuevo = {
        "yt_titulo": str(yt.get("title", it["yt_titulo"]))[:100],
        "yt_desc": str(yt.get("description", it["yt_desc"])),
        "tt": str(tt.get("caption", it["tt"])),
        "ig": str(ig.get("caption", it["ig"])),
        "fb": str(fb.get("caption", it["fb"])),
    }
    for k, v in nuevo.items():
        guardar(nombre, k, v)
    return nuevo


# ------------------------------------------------------------------ paquete manual
def preparar_paquete():
    elegidos = [(n, it) for n, it in ESTADO.items() if it["sel"] and it["plat"]]
    if not elegidos:
        return None, "No hay clips elegidos. Marcá **Elegir este clip** en al menos uno y una plataforma."
    raiz = CARPETA / "publicacion"
    if raiz.exists():
        shutil.rmtree(raiz)
    raiz.mkdir()
    filas, md = [], ["| Clip | Plataformas | Dónde subirlo |", "|---|---|---|"]
    for nombre, it in elegidos:
        d = raiz / Path(nombre).stem
        d.mkdir()
        shutil.copy2(CARPETA / nombre, d / "video.mp4")
        textos = {
            "YouTube": f"TÍTULO:\n{it['yt_titulo']}\n\nDESCRIPCIÓN:\n{it['yt_desc']}\n",
            "TikTok": it["tt"], "Instagram": it["ig"], "Facebook": it["fb"],
        }
        for p in it["plat"]:
            (d / f"{p.lower()}.txt").write_text(textos[p], encoding="utf-8")
            filas.append({"clip": nombre, "plataforma": p, "titulo": it["yt_titulo"] if p == "YouTube" else "",
                          "texto": textos[p].replace("\n", " ⏎ ")})
        md.append(f"| {nombre} | {', '.join(it['plat'])} | " +
                  " · ".join(f"[{p}]({ENLACES[p]})" for p in it["plat"]) + " |")
    with open(raiz / "plan_publicacion.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["clip", "plataforma", "titulo", "texto"])
        w.writeheader()
        w.writerows(filas)
    zip_path = shutil.make_archive(str(CARPETA / "paquete_publicacion"), "zip", raiz)
    md += ["", f"Listo: **{len(elegidos)}** clip(s). Descargá el ZIP de abajo (o mirá la carpeta `{raiz}`). "
               "Cada clip tiene `video.mp4` y un `.txt` por plataforma para copiar y pegar."]
    return zip_path, "\n".join(md)


# ------------------------------------------------------------------ YouTube (opcional)
YT_SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]


def _token_path():
    """El token de YouTube vive junto a panel.py (así no se pierde al cambiar de lote)."""
    fijo = Path(__file__).resolve().parent / "youtube_token.json"
    for p in (fijo, Path.cwd() / "youtube_token.json", BASE / "youtube_token.json"):
        if p.exists():
            return p
    return fijo


def autorizar_youtube(client_secret="client_secret.json", manual=False):
    """Se corre UNA vez. En tu compu abre el navegador. En Colab usá manual=True desde una celda de Python:
         import panel; panel.autorizar_youtube('client_secret.json', manual=True)"""
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(client_secret, YT_SCOPE)
    if manual:
        flow.redirect_uri = "http://localhost:1/"
        url, _ = flow.authorization_url(prompt="consent", access_type="offline")
        print("1) Abrí este link, elegí tu cuenta y aceptá:\n", url)
        print("2) Al final te va a mostrar una página que no carga: es normal.")
        resp = input("3) Copiá la dirección completa (empieza con http://localhost:1/?...) y pegala acá: ").strip()
        flow.fetch_token(authorization_response=resp)
        cred = flow.credentials
    else:
        cred = flow.run_local_server(port=0)
    _token_path().write_text(cred.to_json(), encoding="utf-8")
    print("Listo: guardé", _token_path())


def subir_youtube(nombre, privacidad="private"):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    tp = _token_path()
    if not tp.exists():
        raise RuntimeError("Falta autorizar YouTube (youtube_token.json). Mirá las instrucciones en el encabezado de panel.py.")
    cred = Credentials.from_authorized_user_file(str(tp), YT_SCOPE)
    if not cred.valid:
        cred.refresh(Request())
        tp.write_text(cred.to_json(), encoding="utf-8")
    yt = build("youtube", "v3", credentials=cred)
    it = ESTADO[nombre]
    body = {
        "snippet": {"title": it["yt_titulo"][:100] or Path(nombre).stem, "description": it["yt_desc"][:5000],
                    "categoryId": "22"},
        "status": {"privacyStatus": privacidad, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(CARPETA / nombre), mimetype="video/mp4", chunksize=-1, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return f"https://youtube.com/shorts/{resp['id']}"


def subir_youtube_elegidos(privacidad):
    elegidos = [n for n, it in ESTADO.items() if it["sel"] and "YouTube" in it["plat"]]
    if not elegidos:
        return "No hay clips elegidos con YouTube marcado."
    out = []
    for n in elegidos:
        try:
            out.append(f"✅ {n} → {subir_youtube(n, privacidad)}")
        except Exception as e:
            out.append(f"❌ {n}: {' '.join(str(e).split())[:300]}")
    out.append("\nNota: si tu proyecto de Google no está auditado, YouTube deja los videos como **privados** "
               "sin importar lo elegido. Revisalos en YouTube Studio.")
    return "\n\n".join(out)


# ------------------------------------------------------------------ generar clips desde el panel
def mapear_musica(opcion):
    return {"Arco (tensión, golpe y liberación)": "arco", "Ambiental suave": "auto",
            "Mis pistas (carpeta musica/)": "musica/", "Sin música": "no"}.get(opcion, "arco")


def generar_lote(link, archivo, n, musica, estilo, modelo, enfoque, v):
    """Corre auto_clips.py con lo que cargaste y va mostrando el avance. Al terminar abre el lote nuevo."""
    import gradio as gr
    sk = gr.skip
    fuente = (link or "").strip()
    if not fuente and archivo:
        origen = Path(archivo if isinstance(archivo, str) else getattr(archivo, "name", str(archivo)))
        destino_dir = BASE / "_subidos"
        destino_dir.mkdir(parents=True, exist_ok=True)
        destino = destino_dir / origen.name.replace(" ", "_")
        if not destino.exists():
            shutil.copy2(origen, destino)
        fuente = str(destino)
    if not fuente:
        yield "Pegá un link (Facebook, YouTube, Drive) o subí un video.", v, sk(), sk()
        return
    if not SCRIPT_AUTO.exists():
        yield f"No encuentro {SCRIPT_AUTO}. Dejá auto_clips.py junto a panel.py.", v, sk(), sk()
        return

    salida = ruta_lote(fuente)                       # clips/AAAA-MM/AAAA-MM-DD_HHMM_nombre
    salida.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-u", str(SCRIPT_AUTO), fuente, "--clips", str(int(n)), "--estilo", estilo,
           "--whisper-model", modelo, "--music", mapear_musica(musica), "--out", str(salida)]
    if (enfoque or "").strip():
        cmd += ["--enfoque", enfoque.strip()]
    log = [f"Iniciando... se guardará en {salida.relative_to(BASE)}", "(la transcripción es lo que más tarda)"]
    yield "\n".join(log), v, sk(), sk()

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"}, cwd=str(SCRIPT_AUTO.parent))
    ultimo = 0.0
    for linea in proc.stdout:
        linea = linea.rstrip()
        if not linea:
            continue
        if linea.startswith("frame=") and log and log[-1].startswith("frame="):
            log[-1] = linea                  # el avance del render se actualiza en la misma línea
        else:
            log.append(linea)
        if time.time() - ultimo > 0.7:
            ultimo = time.time()
            yield "\n".join(log[-40:]), v, sk(), sk()
    proc.wait()

    if proc.returncode != 0 or not (salida / "clips.json").exists():
        yield "\n".join(log[-40:]) + "\n\n❌ Falló. Copiá las últimas líneas de arriba si necesitás ayuda.", v, sk(), sk()
        return
    cargar(salida)
    yield ("\n".join(log[-20:]) + f"\n\n✅ Listo: {len(ESTADO)} clips en `{salida.relative_to(BASE)}`. Mirá más abajo."), v + 1, \
        gr.update(choices=list(lotes()), value=etiqueta_de(salida)), gr.update(choices=meses(), value="Todos")


def cambiar_lote(nombre, v):
    ruta = lotes().get(nombre)
    if ruta:
        cargar(ruta)
    return v + 1


def cambiar_mes(mes, v):
    """Filtra el desplegable de lotes por mes y abre el más reciente de ese mes."""
    import gradio as gr
    disponibles = lotes(mes)
    if not disponibles:
        return gr.update(choices=[], value=None), v
    primero = next(iter(disponibles))
    cargar(disponibles[primero])
    return gr.update(choices=list(disponibles), value=primero), v + 1


def zip_mes_ui(mes):
    import gradio as gr
    ruta, msg = zip_mes(mes)
    return gr.update(value=ruta, visible=bool(ruta)), msg


# ------------------------------------------------------------------ publicar por API (con avance en vivo)
def _correr(tareas):
    """Ejecuta tareas [(nombre, fn(progreso))] en segundo plano y va mostrando el avance."""
    q = queue.Queue()

    def trabajo():
        for nombre, fn in tareas:
            try:
                q.put(f"✅ **{nombre}** → {fn(lambda m, n=nombre: q.put(f'⏳ {n}: {m}'))}")
            except Exception as e:
                q.put(f"❌ **{nombre}**: {redes.limpiar(e)}")
        q.put(None)

    threading.Thread(target=trabajo, daemon=True).start()
    lineas = []
    while True:
        m = q.get()
        if m is None:
            break
        if m.startswith("⏳") and lineas and lineas[-1].startswith("⏳"):
            lineas[-1] = m                      # el avance reemplaza al anterior
        else:
            lineas.append(m)
        yield "\n\n".join(lineas)
    yield "\n\n".join(l for l in lineas if not l.startswith("⏳")) or "Sin resultados."


def _elegidos(plataforma):
    return [n for n, it in ESTADO.items() if it["sel"] and plataforma in it["plat"]]


def estado_texto():
    est = redes.estado_conexiones(youtube_ok=_token_path().exists())
    return "\n".join(f"{'🟢' if ok else '⚪'} **{red}**: " + ("conectado" if ok else f"falta {que}")
                     for red, (ok, que) in est.items())


def probar_texto():
    return "\n\n".join(redes.probar_conexiones())


def publicar_facebook(confirmo):
    if not confirmo:
        yield "Marcá la casilla de confirmación: esto publica de verdad en tu página."
        return
    n = _elegidos("Facebook")
    if not n:
        yield "No hay clips elegidos con Facebook marcado."
        return
    yield from _correr([(x, lambda p, x=x: redes.facebook_reel(CARPETA / x, ESTADO[x]["fb"], ESTADO[x]["yt_titulo"], p))
                        for x in n])


def publicar_instagram(confirmo):
    if not confirmo:
        yield "Marcá la casilla de confirmación: esto publica de verdad en tu Instagram."
        return
    n = _elegidos("Instagram")
    if not n:
        yield "No hay clips elegidos con Instagram marcado."
        return
    yield from _correr([(x, lambda p, x=x: redes.instagram_reel(CARPETA / x, ESTADO[x]["ig"], p)) for x in n])


def enviar_tiktok(modo, privacidad, confirmo):
    directo = modo.startswith("Publicación directa")
    if directo and not confirmo:
        yield "Marcá la casilla de confirmación: la publicación directa sube el video de verdad."
        return
    n = _elegidos("TikTok")
    if not n:
        yield "No hay clips elegidos con TikTok marcado."
        return
    yield from _correr([(x, lambda p, x=x: redes.tiktok_enviar(CARPETA / x, ESTADO[x]["tt"],
                                                              "directo" if directo else "borrador", privacidad, p))
                        for x in n])


# ------------------------------------------------------------------ música libre
RESULTADOS_MUSICA = []


def _opciones_musica(items):
    return [f"{i + 1}. {'✅ ' if r.get('instrumental_declarada') else ''}{r['titulo']} — {r['creador']} · {r['seg']} s · {r['licencia']}"
            for i, r in enumerate(items)]


def buscar_musica_ui(consulta, licencia):
    import gradio as gr
    global RESULTADOS_MUSICA
    if not (consulta or "").strip():
        return gr.update(choices=[], value=None), "Escribí qué buscar, por ejemplo: worship piano instrumental, hymn, prayer."
    try:
        RESULTADOS_MUSICA = redes.buscar_musica(consulta.strip(), sin_credito=licencia.startswith("Sin crédito"))
    except Exception as e:
        return gr.update(choices=[], value=None), f"❌ {redes.limpiar(e)}"
    if not RESULTADOS_MUSICA:
        return gr.update(choices=[], value=None), "No encontré pistas con esa búsqueda. Probá en inglés (worship, gospel, hymn, prayer)."
    opciones = _opciones_musica(RESULTADOS_MUSICA)
    return gr.update(choices=opciones, value=opciones[0]), f"{len(opciones)} pistas. Escuchá y elegí una."


def recomendar_musica_ui(ambiente, licencia):
    """Lista recomendada de instrumentales para el ambiente elegido (ordenada por puntaje)."""
    import gradio as gr
    global RESULTADOS_MUSICA
    try:
        RESULTADOS_MUSICA = redes.recomendar_musica(ambiente, sin_credito=licencia.startswith("Sin crédito"))
    except Exception as e:
        return gr.update(choices=[], value=None), f"❌ {redes.limpiar(e)}"
    if not RESULTADOS_MUSICA:
        return gr.update(choices=[], value=None), "No encontré instrumentales para ese ambiente. Probá otro o buscá a mano."
    opciones = _opciones_musica(RESULTADOS_MUSICA)
    return (gr.update(choices=opciones, value=opciones[0]),
            f"{len(opciones)} recomendadas para **{ambiente}**. ✅ = dice ser instrumental. "
            "Ninguna garantía de que no tenga voz: **escuchala antes de usarla**.")


def escuchar_musica_ui(opcion):
    import gradio as gr
    if not opcion:
        return gr.update(value=None), "Elegí una pista de la lista."
    try:
        item = RESULTADOS_MUSICA[int(opcion.split(".")[0]) - 1]
        ruta = redes.previsualizar_musica(item, BASE / "_preview")
    except Exception as e:
        return gr.update(value=None), f"❌ {redes.limpiar(e)}"
    return gr.update(value=str(ruta)), f"Reproduciendo: {item['titulo']} — {item['creador']}"


def descargar_musica_ui(opcion):
    if not opcion:
        return "Elegí una pista de la lista."
    try:
        item = RESULTADOS_MUSICA[int(opcion.split(".")[0]) - 1]
        destino = redes.descargar_musica(item, SCRIPT_AUTO.parent / "musica")   # misma carpeta que usa el generador
    except Exception as e:
        return f"❌ {redes.limpiar(e)}"
    aviso = ("\n\n⚠️ Esta licencia **exige dar crédito**: copiá la atribución del catálogo en la descripción del video."
             if item["requiere_credito"] else "")
    return (f"✅ Guardada en `{destino}` y anotada en `catalogo.txt` con su licencia. Para usarla generá clips con "
            f"**Mis pistas (carpeta musica/)**. Antes de publicar, escuchala.{aviso}")


# ------------------------------------------------------------------ interfaz
ESTILO_LAUNCH = {}      # en Gradio 6 el tema y el css se pasan a launch(); acá se guardan para main()

CSS = """
.gradio-container {max-width: 1200px !important; margin: auto !important;}
.estado-linea p {margin: 0; font-size: 1.05em;}
.tarjeta {border: 1px solid var(--border-color-primary); border-radius: 14px; padding: 6px; margin-bottom: 14px;}
#barra {position: sticky; bottom: 0; z-index: 50; padding: 10px 16px; border-radius: 12px 12px 0 0;
        background: var(--background-fill-primary); border: 1px solid var(--border-color-primary);
        box-shadow: 0 -6px 18px rgba(0,0,0,.12);}
#barra p {margin: 0;}
"""


def resumen_sel(prefijo=""):
    elegidos = [it for it in ESTADO.values() if it["sel"]]
    if not elegidos:
        return "Ningún clip elegido todavía. Marcá **Elegir este clip** en los que quieras publicar."
    por = {p: sum(1 for it in elegidos if p in it["plat"]) for p in PLATAFORMAS}
    return f"{prefijo}**{len(elegidos)}** clip(s) elegido(s) · " + " · ".join(f"{p}: {n}" for p, n in por.items() if n)


def estado_linea():
    est = redes.estado_conexiones(youtube_ok=_token_path().exists())
    return "  ·  ".join(f"{'🟢' if ok else '⚪'} {red}" for red, (ok, _) in est.items())


def construir_app():
    import gradio as gr

    estilo = {"theme": gr.themes.Soft(primary_hue="indigo", neutral_hue="slate", font=["system-ui", "sans-serif"]),
              "css": CSS}
    if "theme" in inspect.signature(gr.Blocks.__init__).parameters:
        kw_blocks = estilo
    else:
        kw_blocks = {}
        ESTILO_LAUNCH.update(estilo)

    with gr.Blocks(title="Panel de clips", **kw_blocks) as demo:
        gr.Markdown(
            "# 🎬 Panel de clips\n"
            "Elegí qué clips publicar, editá los textos de cada plataforma y publicalos o prepará el paquete. "
            "Los textos los propone Gemini; **vos tenés la última palabra**."
        )
        version = gr.State(0)
        resumen_pub = gr.Markdown(value=lambda: resumen_sel("Se publicará: "), render=False)   # se muestra en la sección 3
        with gr.Row(equal_height=True):
            estado_top = gr.Markdown(value=estado_linea, elem_classes="estado-linea")
            btn_estado_top = gr.Button("Actualizar cuentas", size="sm", scale=0)

        with gr.Accordion("1) Generar clips de un video nuevo", open=not ESTADO):
            with gr.Row():
                link = gr.Textbox(label="Link del video (Facebook, YouTube, Drive) o nombre de un archivo",
                                  placeholder="https://www.facebook.com/watch/?v=...", scale=3)
                archivo = gr.File(label="…o subí un video desde tu compu", type="filepath",
                                  file_types=["video"], scale=2)
            with gr.Row():
                n_clips = gr.Slider(1, 10, value=3, step=1, label="Cantidad de clips")
                musica = gr.Dropdown(["Arco (tensión, golpe y liberación)", "Ambiental suave",
                                      "Mis pistas (carpeta musica/)", "Sin música"],
                                     value="Arco (tensión, golpe y liberación)", label="Música")
                estilo_dd = gr.Dropdown(["cine", "vivo"], value="cine", label="Estilo visual")
                modelo = gr.Dropdown(["tiny", "base", "small", "medium"], value="small",
                                     label="Transcripción (más grande = más exacta y más lenta)")
            enfoque = gr.Textbox(label="Enfoque (opcional)", placeholder="Ej: ansiedad y esperanza")
            btn_gen = gr.Button("🎬 Generar clips", variant="primary")
            log_box = gr.Textbox(label="Avance", lines=14, max_lines=14, interactive=False, autoscroll=True)

        gr.Markdown("## 2) Elegí y editá")
        etiquetas = list(lotes())
        actual = etiqueta_de(CARPETA)
        with gr.Row():
            mes_dd = gr.Dropdown(meses(), value="Todos", label="Mes", scale=1, interactive=True)
            lote_dd = gr.Dropdown(etiquetas, value=actual if actual in etiquetas else (etiquetas[0] if etiquetas else None),
                                  label="Videos procesados (año-mes / fecha y hora / nombre)", scale=3, interactive=True)
            btn_zip = gr.Button("📥 ZIP del mes", scale=1)
        zip_msg = gr.Markdown()
        zip_file = gr.File(label="ZIP del mes", visible=False)

        with gr.Row():
            extra = gr.Textbox(label="Indicaciones extra para Gemini (opcional)", scale=4,
                               placeholder="Ej: más corto, tono más cercano, mencioná que hay reunión el domingo")
            todos = gr.Button("✨ Mejorar con Gemini los clips elegidos", scale=1)
        with gr.Row():
            btn_todos = gr.Button("Elegir todos", size="sm")
            btn_ninguno = gr.Button("Quitar selección", size="sm")
        aviso_global = gr.Markdown()

        @gr.render(inputs=version)
        def tarjetas(_v):
            if not ESTADO:
                gr.Markdown("Todavía no hay clips. Generá los primeros arriba con **Generar clips**.")
            for nombre in ESTADO:
                construir_tarjeta(gr, nombre, extra, [contador, resumen_pub])

        with gr.Row(elem_id="barra"):
            contador = gr.Markdown(value=resumen_sel)

        def mejorar_elegidos(indicaciones, v):
            elegidos = [n for n, it in ESTADO.items() if it["sel"]]
            if not elegidos:
                return "Marcá primero **Elegir este clip** en los que quieras mejorar.", v
            msgs = []
            for n in elegidos:
                try:
                    generar_textos(n, indicaciones)
                    msgs.append(f"✅ {n}")
                except Exception as e:
                    msgs.append(f"❌ {n}: {e}")
            return "\n\n".join(msgs), v + 1

        def marcar_todos(valor, v):
            for n in ESTADO:
                ESTADO[n]["sel"] = valor
            guardar("", "", None)                    # solo persiste el estado
            return v + 1, resumen_sel(), resumen_sel("Se publicará: ")

        def refrescar():
            return resumen_sel(), resumen_sel("Se publicará: ")

        todos.click(mejorar_elegidos, [extra, version], [aviso_global, version])
        btn_todos.click(lambda v: marcar_todos(True, v), version, [version, contador, resumen_pub])
        btn_ninguno.click(lambda v: marcar_todos(False, v), version, [version, contador, resumen_pub])

        btn_gen.click(generar_lote, [link, archivo, n_clips, musica, estilo_dd, modelo, enfoque, version],
                      [log_box, version, lote_dd, mes_dd]).then(refrescar, None, [contador, resumen_pub])
        lote_dd.input(cambiar_lote, [lote_dd, version], version).then(refrescar, None, [contador, resumen_pub])
        mes_dd.input(cambiar_mes, [mes_dd, version], [lote_dd, version]).then(refrescar, None, [contador, resumen_pub])
        btn_zip.click(zip_mes_ui, mes_dd, [zip_file, zip_msg])

        with gr.Accordion("🎵 Música instrumental libre (Openverse)", open=False):
            gr.Markdown("Elegí un **ambiente** y te recomiendo instrumentales con licencia abierta, o buscá a mano. "
                        "La licencia de cada pista queda anotada. **Ninguna garantía es absoluta**: igual puede aparecer un reclamo.")
            with gr.Row():
                ambiente = gr.Dropdown(list(redes.AMBIENTES), value="Oración / quietud", label="Ambiente", scale=2)
                lic_musica = gr.Dropdown(["Sin crédito (CC0 / dominio público)", "Con crédito (incluye CC BY)"],
                                         value="Sin crédito (CC0 / dominio público)", label="Licencias", scale=2)
                btn_reco = gr.Button("⭐ Recomendar instrumentales", variant="primary", scale=1)
            with gr.Row():
                q_musica = gr.Textbox(label="…o buscá a mano", placeholder="worship piano instrumental, hymn, prayer ambient", scale=4)
                btn_musica = gr.Button("Buscar", scale=1)
            lista_musica = gr.Dropdown([], label="Resultados (✅ = declarada instrumental)", interactive=True)
            with gr.Row():
                btn_escuchar = gr.Button("▶ Escuchar")
                btn_bajar = gr.Button("Guardar en musica/ (para usarla en los clips)")
            audio_prev = gr.Audio(label="Vista previa", interactive=False)
            msg_musica = gr.Markdown()
            btn_reco.click(recomendar_musica_ui, [ambiente, lic_musica], [lista_musica, msg_musica])
            btn_musica.click(buscar_musica_ui, [q_musica, lic_musica], [lista_musica, msg_musica])
            btn_escuchar.click(escuchar_musica_ui, lista_musica, [audio_prev, msg_musica])
            btn_bajar.click(descargar_musica_ui, lista_musica, msg_musica)

        gr.Markdown("---\n## 3) Publicar")
        with gr.Accordion("🔌 Estado de tus cuentas (detalle)", open=False):
            estado_md = gr.Markdown(value=estado_texto)
            with gr.Row():
                btn_estado = gr.Button("Actualizar")
                btn_probar = gr.Button("Probar Facebook e Instagram")
            probar_md = gr.Markdown()
            btn_estado.click(lambda: (estado_texto(), estado_linea()), None, [estado_md, estado_top])
            btn_estado_top.click(lambda: (estado_texto(), estado_linea()), None, [estado_md, estado_top])
            btn_probar.click(probar_texto, None, probar_md)
        resumen_pub.render()
        confirmo = gr.Checkbox(label="Entiendo que esto publica de verdad en mis cuentas", value=False)

        with gr.Row():
            with gr.Column():
                gr.Markdown("### 📘 Facebook (Reels de tu página)")
                btn_fb = gr.Button("Publicar los clips elegidos en Facebook")
                fb_msg = gr.Markdown()
                btn_fb.click(publicar_facebook, confirmo, fb_msg)
            with gr.Column():
                gr.Markdown("### 📸 Instagram (Reels)")
                btn_ig = gr.Button("Publicar los clips elegidos en Instagram")
                ig_msg = gr.Markdown()
                btn_ig.click(publicar_instagram, confirmo, ig_msg)
        with gr.Row():
            with gr.Column():
                gr.Markdown("### 🎵 TikTok\nEl **borrador** llega a tu bandeja de TikTok y lo publicás desde la app. "
                            "La publicación directa de una app sin auditar solo puede ser privada.")
                tt_modo = gr.Dropdown(["Borrador en mi bandeja (recomendado)", "Publicación directa"],
                                      value="Borrador en mi bandeja (recomendado)", label="Modo")
                tt_priv = gr.Dropdown(["SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS", "PUBLIC_TO_EVERYONE"], value="SELF_ONLY",
                                      label="Privacidad (solo publicación directa)")
                btn_tt = gr.Button("Enviar a TikTok los clips elegidos")
                tt_msg = gr.Markdown()
                btn_tt.click(enviar_tiktok, [tt_modo, tt_priv, confirmo], tt_msg)
            with gr.Column():
                gr.Markdown("### ▶ YouTube\nRequiere autorizar tu canal una vez. "
                            "Un proyecto sin auditar sube **solo en privado**.")
                priv = gr.Dropdown(["private", "unlisted", "public"], value="private", label="Visibilidad pedida")
                btn_yt = gr.Button("Subir a YouTube los clips elegidos")
                yt_msg = gr.Markdown()
                btn_yt.click(subir_youtube_elegidos, priv, yt_msg)

        gr.Markdown("### 📦 O preparar un paquete para subir a mano\nCarpeta y ZIP por clip con el video y los textos "
                    "de cada plataforma, listos para copiar y pegar.")
        btn_paq = gr.Button("Preparar paquete con los clips elegidos")
        paq_msg = gr.Markdown()
        paq_zip = gr.File(label="Descargar paquete (ZIP)")
        btn_paq.click(preparar_paquete, None, [paq_zip, paq_msg])
    return demo


def construir_tarjeta(gr, nombre, extra, resumenes):
    it = ESTADO[nombre]
    clip = it["clip"]
    with gr.Group(elem_classes="tarjeta"):
        gr.Markdown(f"### {nombre}  ·  puntaje {clip.get('score', '?')}/10\n{clip.get('reason', '')}")
        with gr.Row():
            with gr.Column(scale=2):
                gr.Video(value=str(CARPETA / nombre), interactive=False, height=420)
            with gr.Column(scale=3):
                sel = gr.Checkbox(label="Elegir este clip", value=it["sel"])
                plat = gr.CheckboxGroup(PLATAFORMAS, value=it["plat"], label="Publicar en")
                yt_t = gr.Textbox(label="Título (YouTube)", value=it["yt_titulo"], max_lines=2)
                with gr.Accordion("Textos de cada plataforma", open=False):
                    yt_d = gr.Textbox(label="YouTube · descripción", value=it["yt_desc"], lines=3)
                    tt = gr.Textbox(label="TikTok · texto", value=it["tt"], lines=2)
                    ig = gr.Textbox(label="Instagram · texto", value=it["ig"], lines=3)
                    fb = gr.Textbox(label="Facebook · texto", value=it["fb"], lines=2)
                btn = gr.Button("✨ Mejorar este clip con Gemini")
                msg = gr.Markdown()

    def _resumen(campo, v):
        guardar(nombre, campo, v)
        return resumen_sel(), resumen_sel("Se publicará: ")

    sel.change(lambda v: _resumen("sel", v), sel, resumenes)
    plat.change(lambda v: _resumen("plat", v), plat, resumenes)
    for comp, campo in ((yt_t, "yt_titulo"), (yt_d, "yt_desc"), (tt, "tt"), (ig, "ig"), (fb, "fb")):
        comp.blur(lambda v, c=campo: guardar(nombre, c, v), comp, None)

    def mejorar(indicaciones):
        try:
            n = generar_textos(nombre, indicaciones)
            return n["yt_titulo"], n["yt_desc"], n["tt"], n["ig"], n["fb"], "✅ Textos actualizados. Podés editarlos."
        except Exception as e:
            return it["yt_titulo"], it["yt_desc"], it["tt"], it["ig"], it["fb"], f"❌ {e}"

    btn.click(mejorar, extra, [yt_t, yt_d, tt, ig, fb, msg])


def main():
    p = argparse.ArgumentParser(description="Panel web para publicar clips")
    p.add_argument("--carpeta", default="clips", help="carpeta madre: cada video procesado se guarda en su propia subcarpeta")
    p.add_argument("--compartir", action="store_true", help="crear un link público (útil en Colab)")
    p.add_argument("--clave", help="contraseña para entrar al panel (usuario: clips). MUY recomendado con --compartir")
    p.add_argument("--puerto", type=int, default=7860)
    p.add_argument("--autorizar-youtube", metavar="CLIENT_SECRET.JSON", dest="autorizar",
                   help="autorizar tu canal de YouTube (se hace una vez, en tu compu)")
    p.add_argument("--manual", action="store_true", help="con --autorizar-youtube: pegar el código a mano")
    args = p.parse_args()

    os.environ.setdefault("TZ", "America/Argentina/Buenos_Aires")   # Codespaces corre en UTC: así las carpetas llevan tu hora
    if hasattr(time, "tzset"):
        time.tzset()

    global BASE
    BASE = Path(args.carpeta).expanduser().resolve()
    BASE.mkdir(parents=True, exist_ok=True)
    disponibles = lotes()
    cargar(next(iter(disponibles.values())) if disponibles else BASE)
    if args.autorizar:
        autorizar_youtube(args.autorizar, manual=args.manual)
        return
    if args.compartir and not args.clave:
        print("AVISO: creaste un link público SIN clave. Cualquiera con el link podría usar el panel. "
              "Agregá --clave TuClave.")
    demo = construir_app()
    demo.launch(share=args.compartir, server_port=args.puerto, allowed_paths=[str(BASE)],
                auth=("clips", args.clave) if args.clave else None, **ESTILO_LAUNCH)


if __name__ == "__main__":
    main()
