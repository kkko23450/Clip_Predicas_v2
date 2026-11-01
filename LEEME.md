# Clips de prédicas

    python -u panel.py --carpeta clips

1) Generar clips: pegá el link o subí el video. Se guardan en `clips/AAAA-MM/AAAA-MM-DD_HHMM_nombre/` (hora de Argentina).
2) Elegí y editá los textos (Gemini los propone).
3) Publicar: por API (Facebook, Instagram, TikTok, YouTube) o armá el paquete para subir a mano.

Claves: se guardan como secretos del Codespace (Settings > Secrets and variables > Codespaces). Nunca en el código ni en GitHub.
Música libre: sección "Buscar música libre" del panel (Openverse). Guardá la licencia de cada pista.
Cuando termines, cerrá el Codespace para no gastar horas.

Respaldo: el disco del Codespace se borra con el Codespace. Usá el botón **ZIP del mes** para bajarte los videos.
Música instrumental: en el panel, sección 🎵, elegí un ambiente y tocá **Recomendar instrumentales**; escuchá y guardá en `musica/`. Luego generá clips con **Mis pistas**.
Subtítulos: estilo karaoke (blanco con contorno grueso y relleno amarillo que avanza). Usan la fuente Montserrat ExtraBold: sudo apt-get install -y fonts-montserrat. Altura: --pos-sub (0.74 por defecto).

Portadas: al generar los clips, la app saca la MEJOR captura del video original (cara grande, nítida, ojos abiertos) y escribe dos prompts: uno vertical 9:16 y otro horizontal 16:9. Quedan en `portadas/` dentro de cada lote y también en la tarjeta de cada clip (sección 🖼️ Portada). Pasos: bajá la captura, abrí ChatGPT, subí la imagen, pegá el prompt y listo. El texto de la portada (2 a 5 palabras, lo propone Gemini) se edita en el panel y se tocan **Actualizar prompts**. Si ChatGPT te cambia la cara, agregale: "No modifiques el rostro; solo cambiá el fondo y agregá el texto". Revisá siempre que las tildes y los signos ¿ ¡ del texto hayan salido bien. Lotes viejos: botón **Generar portadas que falten**, o `python portadas.py VIDEO.mp4 clips/.../clips.json`. Para no generarlas: `--sin-portadas`.

Silencios y pausas: al renderizar, la app acorta las pausas largas usando los tiempos de cada palabra (no analiza el audio, así que no agrega demora; el corte se hace en el mismo render). Los subtítulos, la música, el clímax y los efectos se acomodan solos a la nueva duración. Niveles (`--silencios`): `suave` (pausas > 0,9 s se acortan a 0,45 s), `normal` (> 0,6 s a 0,3 s, es el predeterminado), `fuerte` (> 0,4 s a 0,2 s) y `no`. También se recorta el silencio del principio y del final. Ajuste fino: `--pausa-umbral 0.7 --pausa-dejar 0.35`. En el panel está el desplegable "Quitar silencios y pausas". Ojo: una pausa dramática de la predicación también se acorta; si querés conservarlas usá `suave`. Los clips quedan unos segundos más cortos que lo que eligió Gemini.
