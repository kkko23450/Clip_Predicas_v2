#!/usr/bin/env python3
"""
clipper.py - De un video largo a clips verticales listos para TikTok / Shorts / Reels.

Requisitos (gratis):
    1) FFmpeg instalado y en el PATH  (https://ffmpeg.org)
    2) pip install faster-whisper numpy
    3) (opcional, para zoom automático hacia la cara) pip install opencv-python-headless

Uso:
    Transcribir:   python clipper.py transcribe mi_predica.mp4
    Renderizar:    python clipper.py render mi_predica.mp4 clips.json
                   python clipper.py render mi_predica.mp4 clips.json --music auto
                   python clipper.py render mi_predica.mp4 clips.json --music musica/ --zoom 1.4 --foco 0.55,0.25

Formato de clips.json:
[
  {"start": "03:12", "end": "03:58",
   "hook": "Si estás cansado de esperar, escuchá esto",
   "title": "Cuando Dios tarda",
   "description": "Texto para la publicación...",
   "hashtags": "#fe #dios #esperanza"}
]
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import wave
from pathlib import Path

# ----------------- ajustes de estilo -----------------
WORDS_PER_CHUNK = 3      # máximo de palabras por pantallazo de subtítulo
MAX_CHARS = 16           # máximo de letras por pantallazo (para que entre en UNA línea)
HOOK_SECONDS = 3.5       # cuánto dura el título de arriba
CTA_SECONDS = 3.0        # cuánto dura el cierre ("Comentá AMÉN")
SUB_X = 540              # centro horizontal del subtítulo
SUB_Y_DEFAULT = 1040     # posición FIJA vertical (1080x1920); ~54 % de la altura
WHITE = "&H00F0F8FF&"    # blanco cálido
AMBER = "&H0000B0FF&"    # palabra activa (ámbar)
GLOW = "&H001E8CFF&"     # brillo naranja alrededor del texto
ZOOM_PATTERN = "gte(mod(t,7),3.5)"   # 3,5 s plano normal / 3,5 s zoom, y se repite

WHISPER_PROMPT = os.environ.get(
    "WHISPER_PROMPT",
    "Predicación cristiana evangélica en español. Dios, Jesús, Cristo, Espíritu Santo, "
    "Biblia, Señor, fe, oración, gracia, salvación, cruz, pecado, perdón, esperanza.",
)


# ---------- utilidades ----------
def ts(t):
    """'03:12', '1:03:12' o 192 -> segundos (float)."""
    if isinstance(t, (int, float)):
        return float(t)
    secs = 0.0
    for p in str(t).split(":"):
        secs = secs * 60 + float(p)
    return secs


def mmss(t):
    m, s = divmod(int(t), 60)
    return f"{m:02d}:{s:02d}"


def ass_time(t):
    t = max(t, 0)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def slug(text):
    keep = "".join(c if c.isalnum() else "_" for c in text.lower())
    return "_".join(x for x in keep.split("_") if x)[:40] or "clip"


def ass_escape(text):
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


# ---------- paso 1: transcribir ----------
def cmd_transcribe(args):
    from faster_whisper import WhisperModel

    video = Path(args.video)
    print(f"Cargando modelo '{args.model}' (la primera vez descarga el modelo)...")
    # auto: usa GPU si hay (Colab/Kaggle), si no CPU. Forzar CPU: WHISPER_DEVICE=cpu
    device = os.environ.get("WHISPER_DEVICE", "auto")
    compute = "int8" if device == "cpu" else "auto"
    model = WhisperModel(args.model, device=device, compute_type=compute)

    # Audio con FFmpeg (16 kHz mono) pasado como arreglo: evita errores de la librería 'av'.
    import numpy as np
    print("Extrayendo audio con FFmpeg...")
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         "-f", "f32le", "-"],
        capture_output=True, check=True,
    ).stdout
    audio = np.frombuffer(raw, dtype=np.float32)

    segments, _ = model.transcribe(
        audio, language=args.lang, word_timestamps=True, vad_filter=True,
        initial_prompt=WHISPER_PROMPT,
    )

    lines, words, segs = [], [], []
    for seg in segments:
        segs.append({"s": round(seg.start, 2), "e": round(seg.end, 2), "t": seg.text.strip()})
        lines.append(f"[{mmss(seg.start)}-{mmss(seg.end)}] {seg.text.strip()}")
        for w in seg.words or []:
            words.append({"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)})
        print(lines[-1])

    video.with_suffix(".transcript.txt").write_text("\n".join(lines), encoding="utf-8")
    video.with_suffix(".words.json").write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
    video.with_suffix(".segments.json").write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
    print(f"\nListo: {video.with_suffix('.transcript.txt')}")


# ---------- subtítulos ASS ----------
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Sub,Arial,68,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,3,0,5,60,60,0,1
Style: Hook,Arial,46,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,8,90,90,230,1
Style: Cta,Arial,56,&H0000FFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,8,90,90,230,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""


def chunk_words(ws):
    """Agrupa palabras en pantallazos cortos que entran en una sola línea."""
    chunks, cur = [], []
    for w in ws:
        txt = w["w"]
        cur_len = sum(len(x["w"]) for x in cur) + max(len(cur) - 1, 0)
        if cur and (len(cur) >= WORDS_PER_CHUNK or cur_len + 1 + len(txt) > MAX_CHARS):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if txt.endswith((".", "?", "!", "…")):   # cortar al final de una frase
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(words, start, end, hook, cta, dur, sub_y=SUB_Y_DEFAULT):
    """Subtítulos que aparecen con un 'pop', resaltan la palabra activa con brillo y desaparecen.
    Posición fija: no se mueven de arriba a abajo."""
    clip_words = [w for w in words if w["s"] >= start - 0.05 and w["e"] <= end + 0.3]
    chunks = chunk_words(clip_words)
    ev = []

    if hook:
        ev.append(
            f"Dialogue: 2,{ass_time(0.05)},{ass_time(min(HOOK_SECONDS, dur))},Hook,,0,0,0,,"
            f"{{\\fad(250,300)}}{ass_escape(hook)}"
        )
    if cta and dur >= 9:
        ev.append(
            f"Dialogue: 2,{ass_time(dur - CTA_SECONDS)},{ass_time(dur - 0.05)},Cta,,0,0,0,,"
            f"{{\\fad(250,200)\\fscx88\\fscy88\\t(0,220,\\fscx100\\fscy100)}}{ass_escape(cta)}"
        )

    for ci, chunk in enumerate(chunks):
        next_start = (chunks[ci + 1][0]["s"] - start) if ci + 1 < len(chunks) else None
        for j, w in enumerate(chunk):
            w_start = max(w["s"] - start, 0)
            if j + 1 < len(chunk):
                w_end = chunk[j + 1]["s"] - start
            else:
                w_end = w["e"] - start + 0.12
                if next_start is not None:
                    w_end = min(w_end, next_start)
            w_end = max(w_end, w_start + 0.05)

            anim = ""
            first, last = j == 0, j == len(chunk) - 1
            if first:   # entra con un "pop" rápido
                anim += "\\fscx82\\fscy82\\t(0,120,\\fscx100\\fscy100)"
            if first or last:
                anim += f"\\fad({40 if first else 0},{70 if last else 0})"
            pos = f"\\an5\\pos({SUB_X},{sub_y})"

            txt = [ass_escape(cw["w"]).upper() for cw in chunk]
            plain = " ".join(txt)
            colored = " ".join(
                f"{{\\c{AMBER if k == j else WHITE}}}{t}" for k, t in enumerate(txt))
            # capa 0: resplandor naranja difuminado; capa 1: texto nítido
            ev.append(f"Dialogue: 0,{ass_time(w_start)},{ass_time(w_end)},Sub,,0,0,0,,"
                      f"{{{pos}{anim}\\bord9\\blur12\\c{GLOW}\\3c{GLOW}}}{plain}")
            ev.append(f"Dialogue: 1,{ass_time(w_start)},{ass_time(w_end)},Sub,,0,0,0,,"
                      f"{{{pos}{anim}}}{colored}")
    return ASS_HEADER + "\n".join(ev) + "\n"


# ---------- música de fondo ----------
PROGRESSIONS = [  # notas MIDI de cada acorde (suave, lento, emotivo)
    [[57, 60, 64], [53, 57, 60], [48, 52, 55], [55, 59, 62]],   # Am  F  C  G
    [[48, 52, 55], [55, 59, 62], [57, 60, 64], [53, 57, 60]],   # C   G  Am F
    [[50, 53, 57], [46, 50, 53], [53, 57, 60], [48, 52, 55]],   # Dm  Bb F  C
]


def make_pad(path, prog, sr=44100, chord_len=9.0):
    """Genera un colchón instrumental ambiental, original y libre de derechos (loop continuo)."""
    import numpy as np
    rng = np.random.default_rng(7)
    L = int(chord_len * sr)
    N = len(prog) * L
    tail = int(4 * sr)
    t = np.arange(L + tail) / sr
    env = np.minimum(1, t / 3.0) * np.minimum(1, (t[-1] - t) / 3.0)
    out = np.zeros((2, N))
    for i, notes in enumerate(prog):
        seg = np.zeros((2, len(t)))
        for m in notes + [notes[0] - 12, notes[0] - 24]:   # + graves para dar cuerpo
            f = 440.0 * 2 ** ((m - 69) / 12)
            for ch in range(2):
                for det, amp in ((1.0, 1.0), (1.003, 0.6), (0.997, 0.6)):
                    ph = rng.random() * 2 * np.pi
                    seg[ch] += amp * (np.sin(2 * np.pi * f * det * t + ph)
                                      + 0.35 * np.sin(2 * np.pi * 2 * f * det * t + ph)
                                      + 0.12 * np.sin(2 * np.pi * 3 * f * det * t + ph))
        seg *= env * (1 + 0.08 * np.sin(2 * np.pi * 0.15 * t + i))
        idx = (i * L + np.arange(len(t))) % N   # suma circular: el loop no tiene cortes
        out[:, idx] += seg
    out /= np.abs(out).max() / 0.9
    pcm = (out.T * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


AUDIO_EXT = (".mp3", ".wav", ".m4a", ".ogg", ".flac")


def audio_files(folder):
    return sorted(f for f in Path(folder).expanduser().iterdir() if f.suffix.lower() in AUDIO_EXT)


def drop_from_envelope(env_db, block_s):
    """Busca el 'golpe': el salto de volumen más fuerte (subida o impacto). Devuelve segundos o None."""
    import numpy as np
    k = max(int(round(0.5 / block_s)), 1)               # promedio de 0,5 s
    n = len(env_db) // k
    if n < 8:
        return None
    e = env_db[: n * k].reshape(n, k).mean(1)
    best, best_i = 0.0, None
    for i in range(3, n - 2):
        step = e[i: i + 3].mean() - e[i - 3: i].mean()   # 1,5 s después vs 1,5 s antes
        if step > best:
            best, best_i = step, i
    return round(best_i * 0.5, 1) if best_i is not None and best >= 5.0 else None


def analyze_track(path, max_s=240):
    """Mide una pista: duración, movimiento, tono y 'golpe' (aprox., hecho con números)."""
    import numpy as np
    path = Path(path)
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True).stdout.strip() or 0)
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-t", str(max_s), "-i", str(path), "-ac", "1", "-ar", "22050", "-f", "f32le", "-"],
        capture_output=True).stdout
    x = np.frombuffer(raw, dtype=np.float32)
    nb = len(x) // 2205                                  # bloques de 0,1 s
    short = 20 * np.log10(np.sqrt((x[: nb * 2205].reshape(nb, 2205) ** 2).mean(1)) + 1e-9)
    move = float(np.std(short)) if nb > 1 else 0.0
    blocks = x[: len(x) // 2048 * 2048].reshape(-1, 2048) * np.hanning(2048)
    spec = np.abs(np.fft.rfft(blocks, axis=1)).mean(0)
    centroid = float((spec * np.fft.rfftfreq(2048, 1 / 22050)).sum() / (spec.sum() + 1e-9))
    return {
        "file": path.name,
        "dur": round(dur),
        "movimiento": "continuo y estable" if move < 2.5 else "con cambios" if move < 5 else "muy cambiante/rítmico",
        "tono": "oscuro/grave" if centroid < 900 else "neutro" if centroid < 2000 else "brillante/agudo",
        "drop": drop_from_envelope(short, 0.1),
    }


def describe_tracks(folder):
    out = []
    for f in audio_files(folder):
        try:
            out.append(analyze_track(f))
        except Exception:
            out.append({"file": f.name, "dur": 0, "movimiento": "?", "tono": "?", "drop": None})
    return out


def pick_music(spec, out_dir, idx, clip=None):
    if not spec or str(spec).lower() in ("no", "none", "sin"):
        return None
    if spec == "auto":
        k = (idx - 1) % len(PROGRESSIONS)
        p = out_dir / "_musica" / f"pad_{k}.wav"
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            print(f"Generando música instrumental ambiental {k + 1}/{len(PROGRESSIONS)}...")
            make_pad(p, PROGRESSIONS[k])
        return str(p.resolve())
    sp = Path(spec).expanduser()
    if sp.is_dir():
        files = audio_files(sp)
        if not files:
            sys.exit(f"No hay audios (mp3/wav/m4a) en la carpeta {sp}")
        wanted = str((clip or {}).get("music") or "").strip().lower()
        if wanted:   # pista elegida automáticamente (o a mano en clips.json)
            for f in files:
                if wanted in (f.name.lower(), f.stem.lower()):
                    print(f"  Música elegida para este clip: {f.name}")
                    return str(f.resolve())
            print(f"  Aviso: no encuentro la pista '{wanted}' en la carpeta; uso una por rotación.")
        return str(files[(idx - 1) % len(files)].resolve())
    if not sp.exists():
        sys.exit(f"No encuentro la música: {sp}")
    return str(sp.resolve())


# ---------- música con arco dramático: tensión -> golpe -> liberación ----------
def make_arc(path, dur, t_drop, sr=44100):
    """Música original a medida del clip: la tensión sube (acorde sin resolver, pulso que se acelera,
    barrido ascendente), un breve silencio, un golpe en t_drop y después una liberación cálida."""
    import numpy as np
    rng = np.random.default_rng(11)
    N = int((dur + 1.0) * sr)
    t = np.arange(N) / sr
    td = float(min(max(t_drop, 3.0), dur - 2.0))
    mtof = lambda m: 440.0 * 2 ** ((m - 69) / 12)

    def tone(f, harm=(1, 0.35, 0.12)):
        ph = rng.random() * 2 * np.pi
        return sum(a * np.sin(2 * np.pi * f * (k + 1) * t + ph) for k, a in enumerate(harm))

    pre = (t < td).astype(float)
    u = np.clip(t / td, 0, 1)                                     # avance de la tensión (0 a 1)

    # 1) tensión: Fa menor sin resolver, crece
    tension = sum(tone(mtof(m) * d) for m in (41, 48, 56, 60) for d in (1.0, 1.004, 0.996))
    tension = tension * (0.15 + 0.85 * u ** 1.5) * pre
    # 2) barrido ascendente + aire
    riser = np.sin(2 * np.pi * np.cumsum(180 + 2600 * u ** 2.5) / sr) * 0.5 \
        + np.diff(rng.standard_normal(N + 1)) * 0.35
    riser = riser * (u ** 3) * pre
    # 3) pulso grave que se acelera
    pulse = np.zeros(N)
    tk = 0.4
    while tk < td - 0.35:
        i, n = int(tk * sr), int(0.22 * sr)
        k = np.arange(min(n, N - i))
        pulse[i: i + len(k)] += np.sin(2 * np.pi * 55 * k / sr) * np.exp(-k / (0.05 * sr)) * (0.3 + 0.7 * tk / td)
        tk += 1.2 - 0.95 * (tk / td) ** 1.4
    pre_mix = (tension * 0.12 + riser * 0.5 + pulse * 1.0)
    pre_mix *= np.clip((td - 0.18 - t) / 0.10, 0, 1)              # ~0,2 s de silencio justo antes del golpe

    # 4) golpe + liberación (acorde mayor cálido, Fa add9)
    post = (t >= td).astype(float)
    ts_ = np.clip(t - td, 0, None)
    boom = np.sin(2 * np.pi * np.cumsum(38 + 70 * np.exp(-ts_ * 6)) / sr) * np.exp(-ts_ * 2.2) * post
    crash = np.diff(rng.standard_normal(N + 1)) * np.exp(-ts_ * 3.0) * post
    rel_env = np.clip(ts_ / 0.6, 0, 1) * np.clip((dur - t) / 1.0, 0, 1) * post
    warm = (sum(tone(mtof(m) * d) for m in (53, 57, 60, 67) for d in (1.0, 1.003, 0.997)) + 2 * tone(mtof(29))) * rel_env

    mono = pre_mix + 0.5 * boom + 0.2 * crash + 0.10 * warm
    right = np.roll(mono, int(0.015 * sr))                        # leve ancho estéreo
    out = np.stack([mono, right])
    out /= max(np.abs(out).max(), 1e-6) / 0.9
    pcm = (out.T * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def resolve_climax(clip, start, dur):
    """Segundo (dentro del clip) donde cae el clímax. Gemini lo marca; si falta, ~65 % del clip."""
    c = clip.get("climax")
    try:
        c = ts(c) if c is not None else None
    except (ValueError, TypeError):
        c = None
    if c is not None:
        rel = c - start if start <= c <= start + dur else c     # acepta segundo absoluto o ya relativo
        if 0.35 * dur <= rel <= dur - 3:
            return rel
    return 0.65 * dur


# ---------- efectos de sonido (generados por código: originales y sin derechos) ----------
def make_sfx(path, dur, cut_times, boom_times, sr=44100, crash_times=()):
    """Pista de efectos: 'whoosh' que llega justo en cada corte y golpes graves de apertura/cierre."""
    import numpy as np
    rng = np.random.default_rng(3)
    N = int((dur + 1.5) * sr)
    out = np.zeros(N)

    def put(sig, t0, gain):
        i = int(max(t0, 0) * sr)
        j = min(i + len(sig), N)
        if j > i:
            out[i:j] += sig[: j - i] * gain

    def whoosh(d=0.5):
        t = np.arange(int(d * sr)) / sr
        u = t / d
        noise = np.diff(rng.standard_normal(len(t) + 1))                  # ruido agudo
        env = np.where(u < 0.75, (u / 0.75) ** 2, np.exp(-(u - 0.75) * 18))  # sube y corta
        chirp = np.sin(2 * np.pi * np.cumsum(250 + 3800 * u ** 2) / sr)   # barrido ascendente
        return (0.55 * noise + 0.35 * chirp) * env

    def boom(d=1.6):
        t = np.arange(int(d * sr)) / sr
        body = np.sin(2 * np.pi * np.cumsum(38 + 70 * np.exp(-t * 6)) / sr) * np.exp(-t * 2.4)
        click = rng.standard_normal(len(t)) * np.exp(-t * 70) * 0.5
        return body + click

    def crash(d=1.8):
        t = np.arange(int(d * sr)) / sr
        return np.diff(rng.standard_normal(len(t) + 1)) * np.exp(-t * 3.0)

    w, b, cr = whoosh(), boom(), crash()
    for ct in crash_times:
        put(cr, ct, 0.45)
    for c in cut_times:
        put(w, c - 0.375, 0.7)     # el pico del whoosh cae justo en el corte
    for bt, g in boom_times:
        put(b, bt, g)
    out /= max(np.abs(out).max(), 1e-6) / 0.9
    pcm = (np.stack([out, out], 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


# ---------- video: recorte, zoom y efectos ----------
def base_graph(mode):
    if mode == "fit":   # video completo sobre fondo desenfocado
        return ("[0:v]fps=30,split[a][b];"
                "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[bg];"
                "[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[base]")
    return "[0:v]fps=30,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920[base]"


def detect_focus(video, start, dur, mode, tmp_dir):
    """Busca la cara en unos cuadros del clip y devuelve (x, y) normalizados, o None."""
    try:
        import cv2
    except ImportError:
        return None
    cascades = [cv2.CascadeClassifier(cv2.data.haarcascades + n)
                for n in ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml")]
    xs, ys = [], []
    for k in range(7):
        t = start + dur * (0.08 + 0.14 * k)
        jpg = Path(tmp_dir) / "_foco.jpg"
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video),
             "-filter_complex", base_graph(mode), "-map", "[base]", "-frames:v", "1", str(jpg)],
            capture_output=True)
        img = cv2.imread(str(jpg)) if r.returncode == 0 else None
        if img is None:
            continue
        H, W = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        found = []
        for ci, c in enumerate(cascades):
            for flip in (False, True):
                g = cv2.flip(gray, 1) if flip else gray
                for (x, y, w, h) in c.detectMultiScale(g, 1.1, 6, minSize=(55, 55)):
                    if flip:
                        x = W - x - w
                    cy = (y + h / 2) / H
                    if cy < 0.40:   # las caras reales están en el 40 % superior; descarta falsos positivos
                        found.append((w * h, (x + w / 2) / W, cy))
        if found:
            _, fx, fy = max(found)
            xs.append(fx)
            ys.append(fy)
    if len(xs) < 2:
        return None
    xs.sort()
    ys.sort()
    return xs[len(xs) // 2], ys[len(ys) // 2]


def zoom_graph(z, fx, fy):
    """Alterna plano normal y zoom hacia (fx, fy) con cortes secos (efecto 'punch-in')."""
    if z <= 1.0:
        return "[base]null[zoomed]"
    W2, H2 = int(1080 * z) // 2 * 2, int(1920 * z) // 2 * 2
    x = int(min(max(fx * W2 - 540, 0), W2 - 1080))
    y = int(min(max(fy * H2 - 0.30 * 1920, 0), H2 - 1920))   # cara en el tercio superior
    return (f"[base]split=2[n][zz];[zz]scale={W2}:{H2},crop=1080:1920:{x}:{y}[z];"
            f"[n][z]overlay=enable='{ZOOM_PATTERN}'[zoomed]")


VOICE_CHAIN = (
    "highpass=f=90,"                                  # saca rumores graves
    "afftdn=nr=18:nf=-28:tn=1,"                       # reduce ruido ambiente
    "equalizer=f=250:t=q:w=1.2:g=-2,"                 # menos "caja"
    "equalizer=f=3200:t=q:w=1:g=3,"                   # más claridad / presencia
    "acompressor=threshold=-21dB:ratio=3:attack=5:release=90:makeup=3"   # voz pareja
)
FMT = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"


def audio_graph(dur, music_idx, sfx_idx, music_vol, sfx_vol, duck_ratio=8):
    fade_out = f"afade=t=out:st={max(dur - 0.6, 0):.2f}:d=0.6"
    master = "loudnorm=I=-13:TP=-1.5:LRA=9"
    parts = [f"[0:a]{FMT},{VOICE_CHAIN}[va]"]
    if music_idx is not None:
        parts += [
            "[va]asplit=2[vo][sc]",
            f"[{music_idx}:a]{FMT},volume={music_vol},afade=t=in:d=1.5[m]",
            # la música baja sola cuando hablan y sube en los silencios (ducking)
            f"[m][sc]sidechaincompress=threshold=0.03:ratio={duck_ratio}:attack=20:release=600[duck]",
        ]
        mix = ["[vo]", "[duck]"]
    else:
        mix = ["[va]"]
    if sfx_idx is not None:
        parts.append(f"[{sfx_idx}:a]{FMT},volume={sfx_vol}[fx]")
        mix.append("[fx]")
    n = len(mix)
    if n == 1:
        parts.append(f"{mix[0]}{fade_out},{master}[a]")
    else:
        parts.append(f"{''.join(mix)}amix=inputs={n}:duration=first:dropout_transition=0,"
                     f"volume={n},{fade_out},{master}[a]")
    return ";".join(parts)


# ---------- paso 3: renderizar ----------
def grade_filter(estilo):
    if estilo == "vivo":
        return "eq=contrast=1.06:saturation=1.15"
    # cine: oscuro, casi blanco y negro, con viñeta
    return "eq=contrast=1.18:saturation=0.3:brightness=-0.02:gamma=0.95,vignette=PI/5"


def cmd_render(args):
    g = lambda k, d=None: getattr(args, k, d)
    video = Path(args.video).resolve()
    words_path = video.with_suffix(".words.json")
    if not words_path.exists():
        sys.exit("No encuentro el .words.json. Corré primero el paso 'transcribe'.")
    words = json.loads(words_path.read_text(encoding="utf-8"))
    clips = json.loads(Path(args.clips).read_text(encoding="utf-8"))

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    zoom = g("zoom", 1.35)
    music_vol = g("music_vol", 0.2)
    sfx_vol = g("sfx_vol", 0.35)
    estilo = g("estilo", "cine")
    sub_y = int(float(g("pos_sub", 0.54)) * 1920)
    cta_default = g("cta", "COMENTÁ AMÉN Y COMPARTILO")
    logo = str(Path(g("logo")).expanduser().resolve()) if g("logo") else None
    manual_focus = None
    if g("foco"):
        fx, fy = (float(v) for v in str(g("foco")).split(","))
        manual_focus = (fx, fy)
    rows = []

    for n, clip in enumerate(clips, 1):
        start, end = ts(clip["start"]), ts(clip["end"])
        dur = end - start
        name = f"{n:02d}_{slug(clip.get('title', 'clip'))}"
        cta = clip.get("cta", cta_default)
        (out_dir / f"{name}.ass").write_text(
            build_ass(words, start, end, clip.get("hook", ""), cta, dur, sub_y), encoding="utf-8")

        # foco del zoom: manual > detección automática > centro-arriba
        focus = manual_focus
        if zoom > 1.0 and not focus:
            focus = detect_focus(video, start, dur, args.mode, out_dir)
            print(f"  Cara detectada en x={focus[0]:.2f} y={focus[1]:.2f}" if focus else
                  "  No detecté la cara: zoom suave centrado (podés indicarla con --foco 0.55,0.25)")
        fx, fy = focus or (0.5, 0.30)
        zoom_clip = zoom if focus else min(zoom, 1.12)   # sin cara detectada: zoom suave y seguro

        # ---- entradas extra de ffmpeg (música, efectos, logo) ----
        extra, idx = [], 1
        spec = g("music")
        climax = resolve_climax(clip, start, dur)
        music, music_ss, duck, vol_mult = None, 0.0, 8, 1.0
        arc_path = None
        if spec == "arco":   # música generada: tensión que sube, golpe en el clímax, liberación
            arc_path = out_dir / f"{name}.arc.wav"
            make_arc(arc_path, dur, climax)
            music, duck, vol_mult = str(arc_path), 3, 1.5
            print(f"  Música con arco dramático: el golpe cae en el segundo {climax:.1f} del clip")
        else:
            music = pick_music(spec, out_dir, n, clip)
            if music and spec != "auto" and not g("sin_alinear", False):
                try:   # desplaza la pista para que su golpe caiga en el clímax del clip
                    drop = analyze_track(music).get("drop")
                    if drop is not None and drop - climax >= 0:
                        music_ss = drop - climax
                        print(f"  Alineo la pista: su golpe (seg {drop:.1f}) cae en el clímax del clip (seg {climax:.1f})")
                except Exception:
                    pass
        music_idx = sfx_idx = logo_idx = None
        if music:
            extra += ["-stream_loop", "-1"] + (["-ss", f"{music_ss:.2f}"] if music_ss > 0 else []) + ["-i", music]
            music_idx, idx = idx, idx + 1
        if not g("sin_sfx", False):
            cuts = ([t for t in (3.5 * k for k in range(1, int(dur / 3.5) + 1)) if t < dur - 0.5]
                    if (zoom > 1.0 and g("whoosh", False)) else [])   # el whoosh en cada zoom ahora es opcional
            booms = [(0.0, 1.0)] + ([(dur - CTA_SECONDS, 0.6)] if cta and dur >= 9 else [])
            crashes = []
            if spec == "arco":   # el golpe del clímax va por esta pista, que NO se baja cuando hablan
                booms.append((climax, 1.0))
                crashes.append(climax)
            sfx_path = out_dir / f"{name}.sfx.wav"
            make_sfx(sfx_path, dur, cuts, booms, crash_times=crashes)
            extra += ["-i", str(sfx_path)]
            sfx_idx, idx = idx, idx + 1
        if logo:
            extra += ["-i", logo]
            logo_idx, idx = idx, idx + 1

        # ---- video ----
        flash = ("" if zoom <= 1.0 or g("sin_destello", False) else
                 ",eq=brightness=0.30:enable='gt(t,0.2)*lt(mod(t,3.5),0.1)'")   # destello en cada corte
        parts = [base_graph(args.mode), zoom_graph(zoom_clip, fx, fy),
                 f"[zoomed]{grade_filter(estilo)}{flash},unsharp=5:5:0.6[g]"]
        if not g("sin_barra", False):   # barra de progreso (ayuda a retener)
            parts += [f"color=c=0xFF9A1F:s=1080x10:r=30:d={dur:.2f}[bar]",
                      f"[g][bar]overlay=x='-w+w*t/{dur:.2f}':y=H-h:shortest=1[b0]"]
        else:
            parts += ["[g]null[b0]"]
        if logo_idx is not None:
            parts += [f"[{logo_idx}:v]scale=150:-1,format=rgba,colorchannelmixer=aa=0.65[lg]",
                      "[b0][lg]overlay=W-w-60:150[b1]"]
        else:
            parts += ["[b0]null[b1]"]
        parts += [f"[b1]ass={name}.ass[v]", audio_graph(dur, music_idx, sfx_idx, music_vol * vol_mult, sfx_vol, duck)]

        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats",
               "-ss", str(start), "-t", str(dur), "-i", str(video)] + extra
        cmd += [
            "-filter_complex", ";".join(parts),
            "-map", "[v]", "-map", "[a]", "-t", f"{dur:.2f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-g", "60",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
            f"{name}.mp4",
        ]
        print(f"\n[{n}/{len(clips)}] {name}  ({dur:.0f}s)")
        subprocess.run(cmd, cwd=out_dir, check=True)
        if not g("sin_sfx", False):
            (out_dir / f"{name}.sfx.wav").unlink(missing_ok=True)
        if arc_path:
            arc_path.unlink(missing_ok=True)

        rows.append({"archivo": f"{name}.mp4", "titulo": clip.get("title", ""),
                     "descripcion": clip.get("description", ""), "hashtags": clip.get("hashtags", "")})

    with open(out_dir / "publicar.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["archivo", "titulo", "descripcion", "hashtags"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nListo. Clips y publicar.csv en: {out_dir}")


def add_render_options(r):
    r.add_argument("--mode", choices=["crop", "fit"], default="crop",
                   help="crop: recorte central | fit: video completo con fondo desenfocado")
    r.add_argument("--estilo", choices=["cine", "vivo"], default="cine",
                   help="cine: oscuro/blanco y negro con viñeta | vivo: colores naturales")
    r.add_argument("--music", default="auto",
                   help="'arco' (tensión que sube, golpe y liberación, a medida de cada clip), 'auto' (colchón ambiental), un mp3, una carpeta con mp3, o 'no'")
    r.add_argument("--music-vol", type=float, default=0.2, dest="music_vol", help="volumen de la música (0.1-0.4)")
    r.add_argument("--sfx-vol", type=float, default=0.35, dest="sfx_vol", help="volumen de los efectos de sonido")
    r.add_argument("--zoom", type=float, default=1.35, help="intensidad del zoom (1 = sin zoom)")
    r.add_argument("--foco", help="dónde está la cara, ej: 0.55,0.25 (x,y entre 0 y 1). Si no, se detecta sola")
    r.add_argument("--pos-sub", type=float, default=0.54, dest="pos_sub",
                   help="altura de los subtítulos (0 arriba, 1 abajo). Default 0.54 = centro")
    r.add_argument("--cta", default="COMENTÁ AMÉN Y COMPARTILO", help="texto final ('' para quitarlo)")
    r.add_argument("--logo", help="imagen PNG con el logo de tu iglesia (marca de agua)")
    r.add_argument("--whoosh", action="store_true", help="agregar sonido 'whoosh' en cada corte de zoom (por defecto NO)")
    r.add_argument("--sin-alinear", action="store_true", dest="sin_alinear",
                   help="no desplazar las pistas de la carpeta para que su golpe caiga en el clímax")
    r.add_argument("--sin-sfx", action="store_true", dest="sin_sfx", help="sin efectos de sonido")
    r.add_argument("--sin-destello", action="store_true", dest="sin_destello", help="sin destello en los cortes")
    r.add_argument("--sin-barra", action="store_true", dest="sin_barra", help="sin barra de progreso")
    r.add_argument("--out", default="clips")


def main():
    p = argparse.ArgumentParser(description="De video largo a clips virales verticales")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("transcribe", help="Transcribe el video con Whisper")
    t.add_argument("video")
    t.add_argument("--model", default="small", help="tiny/base/small/medium/large-v3")
    t.add_argument("--lang", default="es")
    t.set_defaults(func=cmd_transcribe)

    r = sub.add_parser("render", help="Corta y edita los clips")
    r.add_argument("video")
    r.add_argument("clips", help="clips.json")
    add_render_options(r)
    r.set_defaults(func=cmd_render)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
