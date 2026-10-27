#!/usr/bin/env python3
"""
auto_clips.py - Automático de punta a punta:
  video (carpeta local / Drive / link) -> transcribe -> Gemini elige clips con tu guía -> renderiza

Instalación (una vez):
    pip install google-genai faster-whisper gdown yt-dlp opencv-python-headless numpy
    + FFmpeg instalado
    + variable de entorno GEMINI_API_KEY con tu clave (gratis en https://aistudio.google.com/apikey)

Ejemplos:
    python auto_clips.py "C:/Videos/predica.mp4"
    python auto_clips.py "G:/Mi unidad/Predicas/domingo.mp4" --clips 10
    python auto_clips.py "https://drive.google.com/file/d/XXXX/view" --mode fit
    python auto_clips.py "https://youtube.com/watch?v=XXXX" --solo-clips   (solo genera clips.json para revisar)
    python auto_clips.py predica.mp4 --whisper-model medium --retranscribir   (subtítulos más exactos, con GPU)
    python auto_clips.py predica.mp4 --estilo vivo --music musica/ --logo mi_logo.png

Necesita en la misma carpeta: clipper.py y guia_contenido.md
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path

import clipper

HERE = Path(__file__).resolve().parent
VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov", ".m4v")
# 'gemini-flash-latest' apunta siempre al Flash vigente (incluido en el plan gratuito).
# Si Google cambia nombres, probá con --model gemini-3.5-flash o gemini-2.5-flash.
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"]

SYSTEM_BASE = """Sos el editor de contenido de un ministerio cristiano evangélico. Tu trabajo es elegir, \
de la transcripción de una predicación o meditación, los mejores momentos para videos cortos de redes \
sociales (TikTok, YouTube Shorts, Instagram y Facebook Reels) y escribir su gancho, título, descripción y hashtags.

Seguí al pie de la letra la GUÍA DE CONTENIDO de abajo: define la doctrina, el tono y lo que se puede y no se puede hacer.
Los tiempos que elijas deben coincidir con los inicios y finales de los segmentos de la transcripción, \
para que cada clip empiece en una idea completa y termine en un pensamiento cerrado.
Usá solo lo que el predicador realmente dice. No inventes citas ni versículos.

Respondé ÚNICAMENTE con un arreglo JSON válido, sin texto antes ni después y sin bloques de código. Cada elemento:
{
  "start": <segundos de inicio, número>,
  "end": <segundos de fin, número>,
  "hook": "<texto de gancho, máx 12 palabras>",
  "title": "<título corto>",
  "description": "<1 a 2 frases + invitación final>",
  "hashtags": "<5 a 8 hashtags separados por espacio>",
  "score": <1 a 10, potencial de impacto>,
  "reason": "<una frase: por qué este momento funciona>",
  "climax": <segundo ABSOLUTO del video (el mismo formato de la transcripción) donde está la frase o el momento de mayor impacto dentro del clip; debe estar dentro de start y end, preferentemente en el último tercio>,
  "music": "<solo si te paso una lista de pistas: nombre EXACTO del archivo que mejor acompaña este clip>"
}
Si no hay suficientes momentos buenos, devolvé menos clips en lugar de forzar los malos.

=== GUÍA DE CONTENIDO ===
"""


# ---------- 1. conseguir el video ----------
def get_video(source, workdir):
    workdir.mkdir(parents=True, exist_ok=True)
    p = Path(source).expanduser()
    if p.exists():
        return p.resolve()

    if source.startswith("http"):
        # Nombre propio para cada link: así dos videos distintos nunca se pisan ni comparten transcripción.
        tag = hashlib.md5(source.encode()).hexdigest()[:8]
        previo = [f for f in workdir.glob(f"origen_{tag}.*") if f.suffix.lower() in VIDEO_EXT]
        if previo:
            print("Este link ya estaba descargado, lo reutilizo.")
            return previo[0].resolve()
        if "drive.google.com" in source or "docs.google.com" in source:
            import gdown
            out = gdown.download(source, str(workdir / f"origen_{tag}.mp4"), fuzzy=True, quiet=False)
            if not out:
                sys.exit("No pude bajar el archivo de Drive. Verificá que esté compartido como "
                         "'Cualquier persona con el enlace'.")
            return Path(out).resolve()
        r = subprocess.run([
            "yt-dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]",
            "--merge-output-format", "mp4",
            "-o", str(workdir / f"origen_{tag}.%(ext)s"), source,
        ])
        hallados = [f for f in workdir.glob(f"origen_{tag}.*") if f.suffix.lower() in VIDEO_EXT]
        if r.returncode != 0 or not hallados:
            sys.exit("No pude descargar ese link (puede ser privado, requerir inicio de sesión o haber cambiado "
                     "el sitio). Probá actualizar con: pip install -U yt-dlp. Si no, descargá el video a mano "
                     "y pasale el archivo.")
        return hallados[0].resolve()

    sys.exit(f"No encuentro '{source}'. Pasá una ruta existente o un link.")


# ---------- 2. transcribir (con caché) ----------
def ensure_transcript(video, whisper_model, force=False):
    need = [video.with_suffix(s) for s in (".words.json", ".segments.json")]
    if force or not all(f.exists() for f in need):
        clipper.cmd_transcribe(Namespace(video=str(video), model=whisper_model, lang="es"))
    else:
        print("Transcripción ya existente, la reutilizo.")
    return json.loads(video.with_suffix(".segments.json").read_text(encoding="utf-8"))


# ---------- 3. pedirle los clips a Gemini ----------
def music_catalog(args):
    """Si --music es una carpeta, arma la lista de pistas (con medidas y, si existe, tus notas)."""
    spec = getattr(args, "music", None)
    if not spec or str(spec).lower() in ("auto", "no", "none", "sin") or not Path(spec).expanduser().is_dir():
        return ""
    folder = Path(spec).expanduser()
    notes = {}
    cat = folder / "catalogo.txt"   # opcional: una línea por pista -> archivo.mp3, descripción libre
    if cat.exists():
        for line in cat.read_text(encoding="utf-8").splitlines():
            if "," in line:
                name, desc = line.split(",", 1)
                notes[name.strip().lower()] = desc.strip()
    lines = []
    for t in clipper.describe_tracks(folder):
        extra = f" | nota: {notes[t['file'].lower()]}" if t["file"].lower() in notes else ""
        golpe = f" | golpe/subida fuerte en el seg {t['drop']}" if t.get("drop") is not None else ""
        lines.append(f"- {t['file']} | {t['dur']} s | movimiento {t['movimiento']} | tono {t['tono']}{golpe}{extra}")
    print(f"Pistas encontradas: {len(lines)}")
    return "\n".join(lines)


def ask_gemini(segs, args):
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        sys.exit("Falta instalar: pip install google-genai")
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        sys.exit("Falta la variable de entorno GEMINI_API_KEY (clave gratis en aistudio.google.com/apikey).")

    guia = Path(args.guia).read_text(encoding="utf-8")
    system = SYSTEM_BASE + guia

    transcript = "\n".join(f"[{s['s']:.1f}-{s['e']:.1f}] {s['t']}" for s in segs)
    total = segs[-1]["e"]
    user = (
        f"Duración total del video: {total:.0f} segundos.\n"
        f"Elegí hasta {args.clips} clips, cada uno de entre {args.min:.0f} y {args.max:.0f} segundos, "
        f"sin que se solapen entre sí.\n"
    )
    if args.enfoque:
        user += f"Enfoque especial para esta predicación: {args.enfoque}\n"
    tracks = music_catalog(args)
    if tracks:
        user += (
            "\nPISTAS DE MÚSICA DISPONIBLES (elegí una por clip y ponela en el campo \"music\", con el nombre "
            "exacto del archivo). Criterio: esperanza/consuelo -> suave o de piano; confrontación o urgencia -> "
            "más oscura, tensa o con más movimiento; oración -> continua y calma. Evitá repetir la misma pista en clips seguidos si hay otras. "
            "Si una pista tiene golpe/subida fuerte, sirve para clips con un clímax marcado (el script la desplaza para que el golpe caiga en ese momento). Las medidas son aproximadas:\n" + tracks + "\n"
        )
    user += f"\nTRANSCRIPCIÓN (formato [inicio-fin en segundos] texto):\n{transcript}"

    client = genai.Client()  # lee GEMINI_API_KEY del entorno
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",  # fuerza salida JSON válida
        max_output_tokens=16000,
        temperature=0.4,
    )

    # Modelos a probar en orden: el elegido y, si no hay cupo o no existe, otros Flash.
    models = [args.model] + [m for m in FALLBACK_MODELS if m != args.model]
    text, last_err = None, ""
    for model in models:
        print(f"\nConsultando a Gemini ({model})...")
        for attempt in range(1, 4):
            try:
                resp = client.models.generate_content(model=model, contents=user, config=config)
                text = resp.text or ""
                break
            except Exception as e:
                msg = str(e)
                last_err = msg
                short = " ".join(msg.split())[:350]
                print(f"  Error: {short}")
                no_quota = "limit: 0" in msg           # este modelo no tiene cupo gratis
                missing = "404" in msg or "NOT_FOUND" in msg
                temporary = ("429" in msg or "RESOURCE_EXHAUSTED" in msg or "503" in msg
                             or "UNAVAILABLE" in msg)
                if missing or no_quota or not temporary:
                    break                              # pasar al siguiente modelo
                if attempt < 3:
                    wait = 25 * attempt
                    print(f"  Reintento en {wait}s ({attempt}/2)...")
                    time.sleep(wait)
        if text is not None:
            break
        print(f"  Sin resultado con {model}, pruebo el siguiente.")

    if text is None:
        sys.exit("No se pudo usar Gemini con ningún modelo. Último error:\n"
                 + " ".join(last_err.split())[:600])

    a, b = text.find("["), text.rfind("]")
    if a == -1 or b == -1:
        sys.exit(f"La respuesta no vino en JSON:\n{text[:500]}")
    return json.loads(text[a:b + 1])


# ---------- 4. validar y ajustar tiempos ----------
def snap_and_filter(clips, segs, min_d, max_d):
    starts = [s["s"] for s in segs]
    ends = [s["e"] for s in segs]
    total = ends[-1]

    def snap(v, options, tol=4.0):
        best = min(options, key=lambda x: abs(x - v))
        return best if abs(best - v) <= tol else v

    valid = []
    for c in clips:
        try:
            st = snap(clipper.ts(c["start"]), starts)
            en = snap(clipper.ts(c["end"]), ends)
        except (KeyError, ValueError):
            continue
        en = min(en + 0.3, total)
        d = en - st
        if not (min_d - 5 <= d <= max_d + 5) or d > 59.5:
            continue
        c["start"], c["end"] = round(st, 2), round(en, 2)
        valid.append(c)

    valid.sort(key=lambda c: c["start"])
    final = []
    for c in valid:  # si se solapan, queda el de mejor puntaje
        if final and c["start"] < final[-1]["end"]:
            if c.get("score", 0) > final[-1].get("score", 0):
                final[-1] = c
            continue
        final.append(c)
    return final


def main():
    p = argparse.ArgumentParser(description="Video largo -> clips virales con guía evangélica")
    p.add_argument("source", help="ruta local, link de Google Drive o link de video")
    p.add_argument("--clips", type=int, default=8, help="máximo de clips (default 8)")
    p.add_argument("--min", type=float, default=25, help="duración mínima en segundos")
    p.add_argument("--max", type=float, default=55, help="duración máxima en segundos")
    p.add_argument("--enfoque", default="", help="tema a priorizar, ej: 'ansiedad y esperanza'")
    p.add_argument("--guia", default=str(HERE / "guia_contenido.md"))
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--whisper-model", default="small")
    p.add_argument("--retranscribir", action="store_true",
                   help="volver a transcribir aunque ya exista (útil al cambiar --whisper-model)")
    clipper.add_render_options(p)   # estilo, música, efectos, zoom, logo, etc.
    p.add_argument("--solo-clips", action="store_true", help="solo generar clips.json, sin renderizar")
    args = p.parse_args()

    out_dir = Path(args.out).resolve()
    video = get_video(args.source, out_dir / "_descarga")
    segs = ensure_transcript(video, args.whisper_model, args.retranscribir)

    clips = snap_and_filter(ask_gemini(segs, args), segs, args.min, args.max)
    for c in clips:   # texto hablado de cada clip (lo usa el panel para escribir mejores descripciones)
        c["texto"] = " ".join(s["t"] for s in segs if s["s"] >= c["start"] - 0.5 and s["e"] <= c["end"] + 0.5)
    if not clips:
        sys.exit("Gemini no devolvió clips válidos. Probá con otro video o ajustá --min / --max.")

    out_dir.mkdir(parents=True, exist_ok=True)
    clips_path = out_dir / "clips.json"
    clips_path.write_text(json.dumps(clips, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nGemini eligió {len(clips)} clips:")
    for i, c in enumerate(clips, 1):
        print(f" {i}. [{clipper.mmss(c['start'])}-{clipper.mmss(c['end'])}] "
              f"({c.get('score', '?')}/10) {c.get('hook', '')}")
        if c.get("reason"):
            print(f"      -> {c['reason']}")

    if args.solo_clips:
        print(f"\nRevisá/editá {clips_path} y después corré:\n"
              f"  python clipper.py render \"{video}\" \"{clips_path}\"")
        return

    opts = vars(args).copy()
    opts.update(video=str(video), clips=str(clips_path), out=str(out_dir))
    clipper.cmd_render(Namespace(**opts))


if __name__ == "__main__":
    main()
