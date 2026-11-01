#!/usr/bin/env python3
"""
portadas.py - Para cada clip: saca la MEJOR captura del video original y escribe dos prompts
(vertical 9:16 y horizontal 16:9) para pasárselos a ChatGPT junto con esa captura.

Se llama solo al terminar de renderizar cada clip (clipper.py). También se puede correr suelto,
por ejemplo para un lote viejo:

    python portadas.py VIDEO.mp4 clips/2026-10/LOTE/clips.json --out clips/2026-10/LOTE

Resultado, dentro de LOTE/portadas/:
    01_titulo.png                    <- la mejor captura (resolución original del video)
    01_titulo_prompt_vertical.txt    <- prompt 9:16 (TikTok / Reels / Shorts)
    01_titulo_prompt_horizontal.txt  <- prompt 16:9 (YouTube / Facebook)

Cómo elige la captura: prueba ~14 cuadros del clip y puntúa cada uno por tamaño y posición de la
cara, nitidez, ojos abiertos y buena luz. Si no encuentra ninguna cara, elige el cuadro más nítido.
"""
import argparse
import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

N_MUESTRAS = 14          # cuadros que se evalúan por clip
PALABRAS_MAX = 6         # largo máximo del texto de portada

# ---------------------------------------------------------------- plantillas de los prompts
PROMPT_VERTICAL = """\
Te adjunto una captura de un video de una predicación cristiana evangélica. Usala como base para crear una PORTADA VERTICAL en formato 9:16 (1024x1536 o similar) para un video corto de TikTok / Reels / Shorts.

Tema del mensaje: {tema}

Instrucciones:
- Mantené el rostro, los rasgos, la edad y la ropa de la persona EXACTAMENTE como en la foto. No la embellezcas ni la cambies.
- Reencuadrá en vertical con la persona en el centro, de pecho hacia arriba. Si falta imagen a los costados, extendé el fondo de forma natural.
- Estilo cinematográfico: luz cálida dorada, alto contraste, fondo simple y un poco desenfocado, aspecto profesional y emotivo.
- Texto grande en el tercio superior, en mayúsculas, tipografía sans-serif muy gruesa, color blanco con contorno negro grueso, y la palabra más importante en amarillo.
- El texto debe decir EXACTAMENTE: "{texto}"
  Respetá las letras, las tildes y los signos del español. No agregues ninguna otra palabra ni letra.
- Dejá libre el tercio inferior, sin texto, porque lo tapan los botones de la app. Todo lo importante lejos de los bordes.
- Sin logos ni marcas de agua.
"""

PROMPT_HORIZONTAL = """\
Te adjunto una captura de un video de una predicación cristiana evangélica. Usala como base para crear una PORTADA HORIZONTAL en formato 16:9 (apaisada, 1536x1024 o similar) para un video de YouTube / Facebook.

Tema del mensaje: {tema}

Instrucciones:
- Mantené el rostro, los rasgos, la edad y la ropa de la persona EXACTAMENTE como en la foto. No la embellezcas ni la cambies.
- Ubicá a la persona en el tercio derecho de la imagen, de pecho hacia arriba, y dejá el lado izquierdo despejado para el texto. Si falta imagen, extendé el fondo de forma natural.
- Estilo cinematográfico: luz cálida dorada, alto contraste, fondo simple y un poco desenfocado, aspecto profesional y emotivo.
- Texto grande en el lado izquierdo, en mayúsculas, tipografía sans-serif muy gruesa, color blanco con contorno negro grueso, y la palabra más importante en amarillo.
- El texto debe decir EXACTAMENTE: "{texto}"
  Respetá las letras, las tildes y los signos del español. No agregues ninguna otra palabra ni letra.
- Todo lo importante lejos de los bordes, para poder recortar sin perder nada.
- Sin logos ni marcas de agua.
"""


# ---------------------------------------------------------------- utilidades
def _num(v):
    """Segundos a partir de un número o de 'mm:ss' / 'hh:mm:ss'."""
    if isinstance(v, (int, float)):
        return float(v)
    t = 0.0
    for p in str(v).strip().split(":"):
        t = t * 60 + float(p)
    return t


def _slug(text):
    """Mismo nombre que usa clipper.py para los .mp4 (así la portada y el video coinciden)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import clipper
    return clipper.slug(text)


def texto_portada(clip):
    """Texto corto para la portada: el campo 'portada' si existe; si no, el gancho o el título acortados."""
    def limpio(s):
        return re.sub(r"\s+", " ", str(s or "")).strip().strip("\"'“”«»")

    elegido = limpio(clip.get("portada"))
    if not elegido:
        for campo in ("hook", "title"):
            c = limpio(clip.get(campo))
            if c and len(c.split()) <= PALABRAS_MAX:
                elegido = c
                break
    if not elegido:
        c = limpio(clip.get("hook")) or limpio(clip.get("title")) or "MIRÁ ESTE MENSAJE"
        elegido = " ".join(c.split()[:PALABRAS_MAX]).rstrip(",;:-")
    return elegido.upper()


def armar_prompts(clip, texto=None):
    """(prompt_vertical, prompt_horizontal) para este clip."""
    texto = (texto or texto_portada(clip)).strip().upper()
    tema = re.sub(r"\s+", " ", str(clip.get("title") or clip.get("hook") or "mensaje cristiano")).strip()
    return (PROMPT_VERTICAL.format(tema=tema, texto=texto),
            PROMPT_HORIZONTAL.format(tema=tema, texto=texto))


# ---------------------------------------------------------------- elegir la mejor captura
def _extraer(video, t, destino, ancho=None):
    vf = ["-vf", f"scale={ancho}:-2"] if ancho else []
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video),
                        *vf, "-frames:v", "1", str(destino)], capture_output=True)
    return r.returncode == 0 and Path(destino).exists()


def _puntuar(img, cv2, frontal, ojos):
    """Puntaje de un cuadro (más alto = mejor portada). Devuelve (puntaje, tiene_cara)."""
    H, W = img.shape[:2]
    gris = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    nitidez_total = cv2.Laplacian(gris, cv2.CV_64F).var()

    caras = frontal.detectMultiScale(gris, 1.1, 6, minSize=(int(W * 0.05), int(W * 0.05)))
    caras = [c for c in caras if (c[1] + c[3] / 2) / H < 0.60]       # la cara real está en la mitad de arriba
    if len(caras) == 0:
        return min(math.log1p(nitidez_total) / math.log1p(300), 1.0) * 20, False

    x, y, w, h = max(caras, key=lambda c: c[2] * c[3])
    roi = gris[y:y + h, x:x + w]

    tamano = min((w * h) / (W * H) / 0.05, 1.0) * 30                  # cara grande
    nitidez = min(math.log1p(cv2.Laplacian(roi, cv2.CV_64F).var()) / math.log1p(250), 1.0) * 30
    centrado = max(0.0, 1 - abs((x + w / 2) / W - 0.5) / 0.35) * 10   # cerca del centro
    altura = max(0.0, 1 - abs((y + h / 2) / H - 0.33) / 0.30) * 5     # en el tercio superior

    n_ojos = len(ojos.detectMultiScale(roi[: int(h * 0.6)], 1.1, 5, minSize=(max(8, w // 10),) * 2))
    ojos_ab = 15 if n_ojos >= 2 else (5 if n_ojos == 1 else 0)        # ojos cerrados / parpadeo = 0

    brillo = float(roi.mean())
    luz = 10 if 70 <= brillo <= 190 else max(0.0, 10 - abs(brillo - 130) / 10)
    return tamano + nitidez + centrado + altura + ojos_ab + luz, True


def mejor_captura(video, start, end, destino_png, climax=None):
    """Guarda en destino_png el mejor cuadro de [start, end] del video original. Devuelve (segundo, puntaje)."""
    try:
        import cv2
    except ImportError:
        sys.exit("Falta instalar: pip install opencv-python-headless")
    frontal = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    ojos = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")

    start, end = _num(start), _num(end)
    dur = max(end - start, 1.0)
    tiempos = [start + dur * (0.06 + 0.88 * k / (N_MUESTRAS - 1)) for k in range(N_MUESTRAS)]
    mejores = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, t in enumerate(tiempos):
            jpg = Path(tmp) / f"f{i}.jpg"
            if not _extraer(video, t, jpg, ancho=960):
                continue
            img = cv2.imread(str(jpg))
            if img is None:
                continue
            p, hay_cara = _puntuar(img, cv2, frontal, ojos)
            if climax is not None and abs(t - _num(climax)) <= 3:
                p += 4                                    # leve preferencia por el momento fuerte
            mejores.append((p + (100 if hay_cara else 0), p, t))   # con cara siempre gana a sin cara
    if not mejores:
        raise RuntimeError("no pude leer ningún cuadro del video")
    _, puntaje, t_mejor = max(mejores)
    destino_png = Path(destino_png)
    destino_png.parent.mkdir(parents=True, exist_ok=True)
    if not _extraer(video, t_mejor, destino_png):
        raise RuntimeError("no pude guardar la captura")
    return t_mejor, puntaje


# ---------------------------------------------------------------- por clip y por lote
def generar_portada(video, clip, out_dir, name, texto=None):
    """Crea captura + 2 prompts de un clip dentro de out_dir/portadas/. Devuelve dict con las rutas y el texto."""
    pdir = Path(out_dir) / "portadas"
    pdir.mkdir(parents=True, exist_ok=True)
    png = pdir / f"{name}.png"
    t, puntaje = mejor_captura(video, clip["start"], clip["end"], png, clip.get("climax"))
    texto = (texto or texto_portada(clip)).upper()
    pv, ph = armar_prompts(clip, texto)
    (pdir / f"{name}_prompt_vertical.txt").write_text(pv, encoding="utf-8")
    (pdir / f"{name}_prompt_horizontal.txt").write_text(ph, encoding="utf-8")
    return {"png": png, "texto": texto, "segundo": t, "puntaje": puntaje}


def generar_lote(video, clips, out_dir, solo_faltantes=False):
    """Portadas de todos los clips de un lote. Los nombres coinciden con los .mp4 (NN_titulo)."""
    n_ok = 0
    for n, clip in enumerate(clips, 1):
        name = f"{n:02d}_{_slug(clip.get('title', 'clip'))}"
        if solo_faltantes and (Path(out_dir) / "portadas" / f"{name}.png").exists():
            continue
        try:
            r = generar_portada(video, clip, out_dir, name)
            print(f"  Portada {name}: captura del segundo {r['segundo']:.1f} · texto \"{r['texto']}\"")
            n_ok += 1
        except Exception as e:
            print(f"  (No pude armar la portada de {name}: {e})")
    return n_ok


def main():
    p = argparse.ArgumentParser(description="Mejor captura + prompts de portada para cada clip")
    p.add_argument("video", help="video original (el largo, no los clips)")
    p.add_argument("clips", help="clips.json del lote")
    p.add_argument("--out", help="carpeta del lote (por defecto, la carpeta de clips.json)")
    a = p.parse_args()
    clips = json.loads(Path(a.clips).read_text(encoding="utf-8"))
    out = Path(a.out) if a.out else Path(a.clips).resolve().parent
    n = generar_lote(Path(a.video).resolve(), clips, out)
    print(f"\nListo: {n} portada(s) en {out / 'portadas'}")


if __name__ == "__main__":
    main()
