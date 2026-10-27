#!/usr/bin/env python3
"""
redes.py - Publicar clips por las APIs oficiales de Facebook, Instagram y TikTok,
y buscar música libre en Openverse.

Las claves NO van en el código: se leen de variables de entorno
(en Codespaces: Settings > Secrets and variables > Codespaces).

  Facebook ........ FB_PAGE_ID, FB_PAGE_TOKEN
  Instagram ....... IG_USER_ID  (y usa FB_PAGE_TOKEN; o IG_TOKEN si tenés uno propio)
  TikTok .......... TIKTOK_ACCESS_TOKEN
  Versión de la API de Meta (opcional): META_API_VERSION  (por defecto v26.0)

Ayuda para conseguir los datos de Meta (con un token de usuario del Explorador de la API de Graph):
    python redes.py meta-descubrir TOKEN_DE_USUARIO [APP_ID APP_SECRET]
"""
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests

META_VER = os.environ.get("META_API_VERSION", "v26.0")
GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com"
TIKTOK = "https://open.tiktokapis.com"
OPENVERSE = "https://api.openverse.org/v1/audio/"
SECRETOS = ("FB_PAGE_TOKEN", "IG_TOKEN", "TIKTOK_ACCESS_TOKEN")


class ErrorRed(RuntimeError):
    pass


# ------------------------------------------------------------------ utilidades
def limpiar(texto):
    """Quita cualquier clave que se haya colado en un mensaje."""
    texto = " ".join(str(texto).split())
    for nombre in SECRETOS:
        valor = os.environ.get(nombre)
        if valor:
            texto = texto.replace(valor, "***")
    return re.sub(r"(access_token=)[^&\s]+", r"\1***", texto)[:500]


def _env(nombre):
    valor = os.environ.get(nombre, "").strip()
    if not valor:
        raise ErrorRed(f"Falta la variable {nombre}. Guardala como secreto del Codespace.")
    return valor


def _pedir(metodo, url, **kw):
    kw.setdefault("timeout", 120)
    try:
        return requests.request(metodo, url, **kw)
    except requests.RequestException as e:   # el mensaje de requests puede traer la URL con la clave
        raise ErrorRed(f"No se pudo conectar ({type(e).__name__}).") from None


def _json(r, paso):
    try:
        d = r.json()
    except Exception:
        d = {}
    err = d.get("error") if isinstance(d, dict) else None
    if isinstance(err, dict) and str(err.get("code", "")).lower() not in ("", "ok"):   # TikTok responde code="ok" si salió bien
        raise ErrorRed(f"{paso}: {limpiar(err.get('message') or err)}")
    if r.status_code >= 400:
        raise ErrorRed(f"{paso}: la API respondió {r.status_code}. {limpiar(r.text[:300])}")
    return d if isinstance(d, dict) else {}


# ------------------------------------------------------------------ conexiones
def estado_conexiones(youtube_ok=False):
    def ok(*nombres):
        return all(os.environ.get(n, "").strip() for n in nombres)
    return {
        "YouTube": (youtube_ok, "token de YouTube (youtube_token.json)"),
        "Facebook": (ok("FB_PAGE_ID", "FB_PAGE_TOKEN"), "FB_PAGE_ID y FB_PAGE_TOKEN"),
        "Instagram": (ok("IG_USER_ID") and (ok("IG_TOKEN") or ok("FB_PAGE_TOKEN")), "IG_USER_ID y FB_PAGE_TOKEN"),
        "TikTok": (ok("TIKTOK_ACCESS_TOKEN"), "TIKTOK_ACCESS_TOKEN"),
    }


def probar_conexiones():
    """Consulta de verdad a Facebook e Instagram para confirmar que las claves funcionan."""
    out = []
    try:
        d = _json(_pedir("GET", f"{GRAPH}/{META_VER}/{_env('FB_PAGE_ID')}",
                         params={"fields": "name", "access_token": _env("FB_PAGE_TOKEN")}), "Facebook")
        out.append(f"✅ Facebook: página «{d.get('name', '?')}»")
    except ErrorRed as e:
        out.append(f"❌ Facebook: {limpiar(e)}")
    try:
        tok = os.environ.get("IG_TOKEN") or _env("FB_PAGE_TOKEN")
        d = _json(_pedir("GET", f"{GRAPH}/{META_VER}/{_env('IG_USER_ID')}",
                         params={"fields": "username", "access_token": tok}), "Instagram")
        out.append(f"✅ Instagram: @{d.get('username', '?')}")
    except ErrorRed as e:
        out.append(f"❌ Instagram: {limpiar(e)}")
    out.append("ℹ️ TikTok: no tiene prueba sin publicar; se verifica al enviar el primer video.")
    return out


# ------------------------------------------------------------------ Facebook (Reels de página)
def facebook_reel(ruta, descripcion, titulo="", progreso=print):
    """Pasos oficiales: 1) iniciar, 2) subir a rupload.facebook.com, 3) publicar."""
    page, tok = _env("FB_PAGE_ID"), _env("FB_PAGE_TOKEN")
    ruta = Path(ruta)
    tam = ruta.stat().st_size
    progreso("iniciando la subida...")
    d = _json(_pedir("POST", f"{GRAPH}/{META_VER}/{page}/video_reels",
                     data={"upload_phase": "start", "access_token": tok}), "Iniciar")
    vid, url_subida = d.get("video_id"), d.get("upload_url")
    if not vid or not url_subida:
        raise ErrorRed("Iniciar: Facebook no devolvió video_id/upload_url.")

    progreso("subiendo el video...")
    with open(ruta, "rb") as f:
        d = _json(_pedir("POST", url_subida, data=f, headers={
            "Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(tam),
            "Content-Type": "application/octet-stream"}), "Subir")
    if not d.get("success"):
        raise ErrorRed("Subir: Facebook no confirmó la subida.")

    progreso("publicando...")
    datos = {"access_token": tok, "video_id": vid, "upload_phase": "finish", "video_state": "PUBLISHED",
             "description": descripcion[:2000]}
    if titulo:
        datos["title"] = titulo[:100]
    _json(_pedir("POST", f"{GRAPH}/{META_VER}/{page}/video_reels", data=datos), "Publicar")

    for _ in range(30):   # hasta ~5 minutos
        time.sleep(0 if os.environ.get("REDES_SIN_ESPERA") else 10)
        try:
            est = _json(_pedir("GET", f"{GRAPH}/{META_VER}/{vid}",
                               params={"fields": "status", "access_token": tok}), "Estado").get("status", {})
        except ErrorRed:
            continue
        pub = (est.get("publishing_phase") or {})
        proc = (est.get("processing_phase") or {})
        if est.get("video_status") == "error" or pub.get("status") == "error" or proc.get("status") == "error":
            msg = (proc.get("error") or pub.get("error") or {}).get("message", "error desconocido")
            raise ErrorRed(f"Facebook rechazó el video: {limpiar(msg)}")
        if pub.get("status") == "complete" or pub.get("publish_status") == "published":
            return f"https://www.facebook.com/reel/{vid}"
        progreso("Facebook está procesando el video...")
    return f"https://www.facebook.com/reel/{vid} (todavía procesando; revisalo en unos minutos)"


# ------------------------------------------------------------------ Instagram (Reels)
def instagram_reel(ruta, texto, progreso=print):
    """Pasos oficiales: 1) crear contenedor reanudable, 2) subir a rupload, 3) esperar, 4) publicar."""
    ig = _env("IG_USER_ID")
    tok = os.environ.get("IG_TOKEN", "").strip() or _env("FB_PAGE_TOKEN")
    ruta = Path(ruta)
    tam = ruta.stat().st_size
    progreso("creando el contenedor...")
    d = _json(_pedir("POST", f"{GRAPH}/{META_VER}/{ig}/media", data={
        "media_type": "REELS", "upload_type": "resumable", "caption": texto[:2200], "access_token": tok}), "Crear")
    cid = d.get("id")
    if not cid:
        raise ErrorRed("Crear: Instagram no devolvió el id del contenedor.")

    progreso("subiendo el video...")
    with open(ruta, "rb") as f:
        d = _json(_pedir("POST", f"{RUPLOAD}/ig-api-upload/{META_VER}/{cid}", data=f, headers={
            "Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(tam)}), "Subir")
    if not d.get("success", True):
        raise ErrorRed("Subir: Instagram no confirmó la subida.")

    for _ in range(40):   # hasta ~10 minutos
        time.sleep(0 if os.environ.get("REDES_SIN_ESPERA") else 15)
        est = _json(_pedir("GET", f"{GRAPH}/{META_VER}/{cid}",
                           params={"fields": "status_code,status", "access_token": tok}), "Estado")
        codigo = est.get("status_code")
        if codigo == "FINISHED":
            break
        if codigo in ("ERROR", "EXPIRED"):
            raise ErrorRed(f"Instagram rechazó el video ({codigo}): {limpiar(est.get('status', ''))}")
        progreso("Instagram está procesando el video...")
    else:
        raise ErrorRed("Instagram tardó demasiado en procesar el video. Probá de nuevo más tarde.")

    progreso("publicando...")
    d = _json(_pedir("POST", f"{GRAPH}/{META_VER}/{ig}/media_publish",
                     data={"creation_id": cid, "access_token": tok}), "Publicar")
    media = d.get("id", "?")
    try:
        link = _json(_pedir("GET", f"{GRAPH}/{META_VER}/{media}",
                            params={"fields": "permalink", "access_token": tok}), "Link").get("permalink")
    except ErrorRed:
        link = None
    return link or f"publicado (id {media})"


# ------------------------------------------------------------------ TikTok
def tiktok_enviar(ruta, titulo, modo="borrador", privacidad="SELF_ONLY", progreso=print):
    """modo='borrador': el video llega a tu bandeja de TikTok y lo publicás desde la app.
       modo='directo': publicación directa (una app sin auditar solo puede publicar en privado)."""
    tok = _env("TIKTOK_ACCESS_TOKEN")
    ruta = Path(ruta)
    tam = ruta.stat().st_size
    if tam > 64 * 1024 * 1024:
        raise ErrorRed("El video pesa más de 64 MB; esta versión lo envía en una sola parte.")
    cab = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=UTF-8"}
    origen = {"source": "FILE_UPLOAD", "video_size": tam, "chunk_size": tam, "total_chunk_count": 1}

    if modo == "directo":
        progreso("consultando tu cuenta...")
        info = _json(_pedir("POST", f"{TIKTOK}/v2/post/publish/creator_info/query/", headers=cab), "Cuenta")
        opciones = (info.get("data") or {}).get("privacy_level_options") or []
        if privacidad not in opciones:
            raise ErrorRed(f"La privacidad {privacidad} no está permitida para tu cuenta. Opciones: {', '.join(opciones) or 'ninguna'}.")
        url = f"{TIKTOK}/v2/post/publish/video/init/"
        cuerpo = {"post_info": {"title": titulo[:2200], "privacy_level": privacidad}, "source_info": origen}
    else:
        url = f"{TIKTOK}/v2/post/publish/inbox/video/init/"
        cuerpo = {"source_info": origen}

    progreso("iniciando el envío...")
    d = (_json(_pedir("POST", url, headers=cab, json=cuerpo), "Iniciar").get("data") or {})
    subida, publish_id = d.get("upload_url"), d.get("publish_id")
    if not subida:
        raise ErrorRed("Iniciar: TikTok no devolvió el upload_url.")

    progreso("subiendo el video...")
    with open(ruta, "rb") as f:
        r = _pedir("PUT", subida, data=f, headers={
            "Content-Type": "video/mp4", "Content-Length": str(tam), "Content-Range": f"bytes 0-{tam - 1}/{tam}"})
    if r.status_code >= 400:
        raise ErrorRed(f"Subir: TikTok respondió {r.status_code}. {limpiar(r.text[:200])}")

    estado = ""
    try:
        e = _json(_pedir("POST", f"{TIKTOK}/v2/post/publish/status/fetch/", headers=cab,
                         json={"publish_id": publish_id}), "Estado").get("data") or {}
        estado = e.get("status", "")
    except ErrorRed:
        pass
    if modo == "borrador":
        return f"enviado a tu bandeja de TikTok (id {publish_id}). Abrí la app de TikTok, terminá la edición y pegá el texto. {estado}".strip()
    return f"enviado (id {publish_id}, privacidad {privacidad}). {estado}".strip()


# ------------------------------------------------------------------ ayuda para obtener datos de Meta
def meta_descubrir(token_usuario, app_id=None, app_secret=None):
    """Con un token de usuario, lista tus páginas, sus tokens y el ID de Instagram vinculado."""
    if app_id and app_secret:   # token de larga duración: los tokens de página derivados no vencen
        r = _json(_pedir("GET", f"{GRAPH}/{META_VER}/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": app_secret,
            "fb_exchange_token": token_usuario}), "Token largo")
        token_usuario = r.get("access_token", token_usuario)
    d = _json(_pedir("GET", f"{GRAPH}/{META_VER}/me/accounts", params={
        "fields": "id,name,access_token,instagram_business_account{id,username}",
        "access_token": token_usuario}), "Páginas")
    paginas = d.get("data", [])
    if not paginas:
        print("No encontré páginas. Revisá que el token tenga los permisos pages_show_list y pages_read_engagement.")
    for p in paginas:
        print(f"\nPágina: {p.get('name')}")
        print(f"  FB_PAGE_ID={p.get('id')}")
        print(f"  FB_PAGE_TOKEN={p.get('access_token')}")
        ig = p.get("instagram_business_account")
        print(f"  IG_USER_ID={ig['id']}  (@{ig.get('username', '?')})" if ig
              else "  (esta página no tiene una cuenta de Instagram profesional vinculada)")
    print("\nGuardá cada valor como secreto del Codespace. No los pegues en chats ni los subas a GitHub.")


# ------------------------------------------------------------------ música libre (Openverse)
def buscar_musica(consulta, sin_credito=True, cantidad=15):
    """Busca música con licencia abierta. sin_credito=True: solo CC0 y dominio público."""
    lic = "cc0,pdm" if sin_credito else "cc0,pdm,by"
    r = _pedir("GET", OPENVERSE, params={"q": consulta, "license": lic, "category": "music", "page_size": cantidad},
               headers={"User-Agent": "clips-predicas/1.0"}, timeout=40)
    if r.status_code == 429:
        raise ErrorRed("Openverse pidió esperar un momento (límite de consultas). Probá en un minuto.")
    d = _json(r, "Búsqueda")
    out = []
    for it in d.get("results", []):
        if not it.get("url"):
            continue
        lic_it = f"{str(it.get('license', '')).upper()} {it.get('license_version', '')}".strip()
        out.append({
            "titulo": it.get("title") or "sin título", "creador": it.get("creator") or "desconocido",
            "licencia": lic_it, "licencia_url": it.get("license_url", ""), "fuente": it.get("source") or it.get("provider", ""),
            "url": it["url"], "pagina": it.get("foreign_landing_url", ""),
            "seg": round((it.get("duration") or 0) / 1000), "formato": (it.get("filetype") or "mp3").lower(),
            "atribucion": it.get("attribution", ""), "requiere_credito": str(it.get("license", "")).lower().startswith("by"),
            "tags": [str(t.get("name", "")) for t in (it.get("tags") or []) if isinstance(t, dict)],
            "generos": [str(g) for g in (it.get("genres") or [])],
        })
    return out


def descargar_musica(item, carpeta="musica"):
    """Baja la pista a la carpeta musica/ y la anota en catalogo.txt con su licencia y fuente."""
    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    crudo = unicodedata.normalize("NFKD", f"{item['titulo']}_{item['creador']}").encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "_", crudo).strip("_")[:50] or "pista"
    destino = carpeta / f"{base}.{item['formato'] if item['formato'] in ('mp3', 'wav', 'ogg', 'm4a', 'flac') else 'mp3'}"
    with _pedir("GET", item["url"], stream=True, timeout=120, headers={"User-Agent": "clips-predicas/1.0"}) as r:
        if r.status_code >= 400:
            raise ErrorRed(f"No pude descargar la pista (error {r.status_code}).")
        with open(destino, "wb") as f:
            for trozo in r.iter_content(65536):
                f.write(trozo)
    nota = (f"{item['titulo']} de {item['creador']} | fuente: Openverse/{item['fuente']} | licencia: {item['licencia']} "
            f"{item['licencia_url']} | página: {item['pagina']}")
    if item["requiere_credito"]:
        nota += f" | REQUIERE DAR CRÉDITO: {item['atribucion']}"
    with open(carpeta / "catalogo.txt", "a", encoding="utf-8") as f:
        f.write(f"{destino.name}, {nota}\n")
    return destino


# ------------------------------------------------------------------ recomendador de instrumentales
AMBIENTES = {
    "Oración / quietud": ["prayer ambient instrumental", "peaceful piano instrumental", "meditation ambient"],
    "Adoración": ["worship instrumental", "worship piano instrumental", "christian instrumental"],
    "Esperanza / luz": ["hopeful piano instrumental", "uplifting cinematic instrumental", "inspirational piano"],
    "Tensión y llamado": ["dramatic cinematic instrumental", "dark ambient cinematic", "emotional strings instrumental"],
    "Himnos / gospel": ["hymn instrumental", "organ hymn", "gospel piano instrumental"],
}
_VOCAL = re.compile(r"\b(vocals?|lyrics?|feat\.?|ft\.?|sings?|singing|singer|rap|choir|voice|acapella|a cappella)\b", re.I)
_ESTILO = ("piano", "ambient", "cinematic", "strings", "orchestral", "pad", "organ", "acoustic", "guitar")


def puntuar_musica(it, minimo=60, maximo=420):
    """Puntaje para recomendar: prioriza 'instrumental' declarado, duración útil y licencia sin crédito.
    Devuelve None si conviene descartarla (parece tener voz o dura muy poco/mucho)."""
    texto = " ".join([it["titulo"], " ".join(it.get("tags", [])), " ".join(it.get("generos", []))])
    if _VOCAL.search(texto):
        return None
    seg = it["seg"]
    if seg and not (minimo <= seg <= maximo):
        return None
    t = texto.lower()
    pts = 3 if "instrumental" in t else 0
    pts += 1 if any(k in t for k in _ESTILO) else 0
    pts += 1 if 90 <= seg <= 240 else 0
    pts += 1 if not it["requiere_credito"] else 0
    return pts


def recomendar_musica(ambiente, sin_credito=True, cantidad=12):
    """Busca varias consultas del ambiente elegido, junta, quita repetidas y ordena por puntaje."""
    consultas = AMBIENTES.get(ambiente) or AMBIENTES["Oración / quietud"]
    vistos, candidatas, error = set(), [], None
    for i, q in enumerate(consultas):
        if i:
            time.sleep(0.6)       # Openverse limita las consultas sin clave
        try:
            for it in buscar_musica(q, sin_credito=sin_credito, cantidad=20):
                if it["url"] in vistos:
                    continue
                vistos.add(it["url"])
                pts = puntuar_musica(it)
                if pts is not None:
                    it["puntaje"] = pts
                    it["instrumental_declarada"] = "instrumental" in (it["titulo"] + " ".join(it["tags"]) + " ".join(it["generos"])).lower()
                    candidatas.append(it)
        except ErrorRed as e:
            error = e
    if not candidatas and error:
        raise error
    candidatas.sort(key=lambda x: (-x["puntaje"], x["seg"]))
    return candidatas[:cantidad]


def previsualizar_musica(item, carpeta):
    """Baja la pista a una carpeta temporal para escucharla en el panel antes de elegirla."""
    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = carpeta / f"prev_{abs(hash(item['url'])) % 10**8}.{item['formato'] if item['formato'] in ('mp3', 'wav', 'ogg', 'm4a', 'flac') else 'mp3'}"
    if not destino.exists():
        with _pedir("GET", item["url"], stream=True, timeout=120, headers={"User-Agent": "clips-predicas/1.0"}) as r:
            if r.status_code >= 400:
                raise ErrorRed(f"No pude bajar la pista para escucharla (error {r.status_code}).")
            with open(destino, "wb") as f:
                for trozo in r.iter_content(65536):
                    f.write(trozo)
    return destino


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "meta-descubrir":
        meta_descubrir(sys.argv[2], *(sys.argv[3:5] if len(sys.argv) >= 5 else (None, None)))
    else:
        print(__doc__)
