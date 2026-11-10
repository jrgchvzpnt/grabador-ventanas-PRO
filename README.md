# Grabador de ventanas

Aplicación de escritorio en Python (Tkinter) para **Windows** que permite
seleccionar una ventana abierta (por ejemplo, una ventana o pestaña del
navegador), grabarla en video, capturar el audio del sistema, pausar y
reanudar la grabación, ver una vista previa y un cronómetro en vivo
mientras grabas, y reproducir el resultado al finalizar.

El video se codifica **en tiempo real** mientras grabas (no al final), así
que al pulsar "Detener" el archivo queda listo casi al instante — no hay
que esperar una recompresión larga.
La ventana de la aplicación usa una vista previa compacta para mostrar
todos sus controles. Al detener la grabación, la mezcla de audio se procesa
en segundo plano y muestra el porcentaje de avance sin bloquear la interfaz.
En esa mezcla se comparan las duraciones de las pistas y se compensa la
deriva acumulada entre los relojes de captura; el video no se recodifica.

## Estructura del proyecto

- `app.py`: interfaz y coordinación de la grabación; se sigue iniciando con
  `python app.py`.
- `config.py`: opciones de captura, formatos, perfiles de calidad y sus
  estimaciones de tamaño.
- `audio.py`: captura de audio loopback WASAPI y escritura de la pista WAV.
- `drive.py`: listado y descarga secuencial de MP4 desde carpetas públicas
  de Google Drive y lectura de su duración.
- `media.py`: combinación de audio y video y compensación de desfase entre
  sus duraciones.

## Características

- **Selección de ventana**: lista todas las ventanas abiertas del sistema y
  permite elegir cuál grabar.
- **Abrir video por URL (opcional)**: un campo para pegar un link (por
  ejemplo, un video de Google Drive) que se abre con tu navegador y tu
  sesión normal. La app **no maneja usuarios ni contraseñas**: solo llama
  a `webbrowser.open()`, así que inicias sesión tú mismo si hace falta.
- **Importar videos desde Google Drive**: permite analizar una carpeta
  compartida públicamente, listar sus MP4 y descargarlos secuencialmente
  conservando el nombre y la estructura de subcarpetas. La duración se
  lee después de descargar cada archivo. El campo **URL Drive** está en la
  ventana principal. Si Drive responde que requiere acceso, la app abre el
  enlace en el navegador para que el usuario inicie sesión o revise permisos.
  La app no lee cookies ni usa esa sesión: para descargar, la carpeta debe
  estar compartida como **Cualquier persona con el enlace**.
- **Grabación de video**: captura la región de pantalla donde está la
  ventana seleccionada usando [`mss`](https://pypi.org/project/mss/) y
  [`OpenCV`](https://pypi.org/project/opencv-python/). La aplicación activa
  la conciencia DPI de Windows para que los límites de ventana coincidan
  con los píxeles físicos capturados, también al usar escalado de pantalla.
- **Grabación de audio del sistema**: captura el audio de salida del
  dispositivo predeterminado (loopback WASAPI) usando
  [`PyAudioWPatch`](https://pypi.org/project/PyAudioWPatch/). La captura de
  video espera a que el dispositivo de audio esté listo para iniciar ambos
  flujos juntos.
- **Control de volumen de la PC**: una barra deslizante controla el
  volumen general de salida de Windows (usando
  [`pycaw`](https://pypi.org/project/pycaw/)) sin salir de la app ni
  interrumpir el video que estás viendo/grabando.
- **Pausa / reanudar**: pausa la grabación (video y audio) sin detenerla;
  el tiempo en pausa no queda incluido en el archivo final.
- **Cronómetro en vivo**: muestra el tiempo grabado (MM:SS.mmm, u
  HH:MM:SS.mmm si pasa de una hora) desde que arranca la grabación,
  descontando el tiempo que estuvo en pausa.
- **Detener automáticamente**: elige horas, minutos y segundos con los
  controles de tiempo; al alcanzarlo, detiene y guarda la grabación. Si el
  valor está en `00:00:00`, la grabación se detiene manualmente.
- **Sincronizar Play/Pausa del video**: al iniciar, envía la tecla Espacio a
  la ventana seleccionada y coordina esa pulsación con el comienzo de la
  captura. El botón **Pausar/Reanudar** también pausa o reproduce el video
  de la ventana junto con la grabación. Úsalo con un reproductor que
  controle Play/Pausa con Espacio; si el estado del reproductor no coincide
  con el de la grabación, la tecla puede invertirlo.
- **Formato de video**: un combo para elegir el formato de salida entre
  los más populares — **MP4, MKV, MOV, AVI y WEBM**. Cada uno usa el
  códec de video/audio más adecuado para ese contenedor (por ejemplo
  H.264+AAC para MP4/MKV/MOV, Xvid+MP3 para AVI, VP9+Opus para WEBM).
- **Calidad**: elige **Alta, Media o Baja**. La estimación de tamaño para
  10 minutos se actualiza al cambiar el formato o la calidad. El perfil
  Alta graba a 1920×1080 y 30 fps (ajusta la imagen sin deformarla; puede
  agregar bordes negros si la ventana tiene otra proporción). Media y Baja
  conservan la resolución de la ventana y graban a 15 fps.
- **Guardar como...**: permite elegir la carpeta y el nombre del archivo
  antes de grabar. La extensión queda siempre forzada al formato elegido
  en el combo, sin importar lo que se escriba en el diálogo.
- **Vista previa en vivo**: muestra en la propia ventana de la app el
  contenido que se está grabando en tiempo real, con un indicador
  "PAUSADO" superpuesto cuando corresponde.
- **Codificación en tiempo real**: el video se codifica cuadro a cuadro
  mientras grabas (mediante un pipe a `ffmpeg`), ya en el códec final del
  formato elegido. Si una captura de pantalla se retrasa, se conserva el
  ritmo de cuadros para reducir diferencias de duración con el audio. Al
  finalizar con audio, se corrige también la deriva de duración entre las
  pistas. Al pulsar "Detener": si no grabaste audio, el archivo
  ya está listo al instante; si grabaste audio, solo falta una mezcla
  rápida (el video se copia tal cual, solo se codifica el audio), mucho más
  veloz que recomprimir todo el video de nuevo.
- **Reproducción**: botón para abrir la grabación resultante con el
  reproductor de video predeterminado de Windows.

## Requisitos

- Windows (usa APIs específicas de Windows: `pywin32`, `pygetwindow`,
  WASAPI loopback).
- Python 3.9 o superior.
- No necesitas instalar `ffmpeg` por separado: se incluye automáticamente
  mediante el paquete `imageio-ffmpeg`.

## Instalación

```bash
pip install -r requirements.txt
```

Dependencias incluidas en `requirements.txt`:

| Paquete           | Uso                                                   |
|-------------------|--------------------------------------------------------|
| `pygetwindow`     | Listar y manipular ventanas abiertas                   |
| `pywin32`         | Obtener el rectángulo exacto de la ventana (WinAPI)    |
| `opencv-python`   | Escritura de frames a video                            |
| `mss`             | Captura rápida de pantalla/región                      |
| `numpy`           | Manipulación de los frames capturados                  |
| `PyAudioWPatch`   | Captura de audio del sistema vía loopback WASAPI       |
| `imageio-ffmpeg`  | Binario de `ffmpeg` listo para usar (sin instalación aparte) |
| `Pillow`          | Generar la imagen de la vista previa en la interfaz    |
| `pycaw`           | Leer y ajustar el volumen general de Windows           |
| `comtypes`        | Dependencia de `pycaw` para llamar a las APIs COM de Windows |
| `gdown`           | Enumerar y descargar archivos de carpetas públicas de Google Drive |
| `requests`        | Conexiones HTTPS para la descarga pública desde Drive |

## Uso

```bash
python app.py
```

1. Abre la ventana/pestaña del navegador (o cualquier otra) que quieras
   grabar. Si es un video con link (por ejemplo de Google Drive), puedes
   pegarlo en el campo **"Abrir video (URL, opcional)"** y pulsar
   **Abrir**: se abrirá con tu navegador y tu sesión ya iniciada. Si Drive
   te pide iniciar sesión, hazlo tú mismo en esa pestaña (la app nunca ve
   ni guarda tu usuario o contraseña).
   Para importar una carpeta pública, pega su enlace en **URL Drive** y
   pulsa **Importar MP4**. Analiza la carpeta y luego pulsa
   **Descargar todos los MP4 en orden**. El proceso conserva los nombres y
   subdirectorios que Drive reporte, no sobrescribe archivos existentes y
   muestra la duración estimada a partir de los metadatos de cada MP4.
   Si Drive indica que requiere acceso, la app abrirá el enlace en el navegador
   para que inicies sesión o revises permisos. El inicio de sesión no se comparte
   con la aplicación: habilita **Cualquier persona con el enlace** y vuelve a
   analizar para descargar.
2. En la app, pulsa **Actualizar** si la ventana no aparece en la lista, y
   selecciónala en el desplegable.
3. (Opcional) Marca o desmarca **"Grabar también el audio del sistema"**.
   La opción **"Sincronizar Play/Pausa del video"** está habilitada por
   defecto: deja el video pausado para que la app envíe Espacio al iniciar.
   Durante la grabación, **Pausar/Reanudar** enviará Espacio para pausar o
   reanudar también el video. Desmárcala si el reproductor no usa esa tecla.
4. (Opcional) Ajusta la barra **"Volumen de la PC"** para subir o bajar
   el volumen general de Windows sin interrumpir el video ni salir de la
   app.
5. (Opcional) Elige el **Formato de video** (MP4, MKV, MOV, AVI o WEBM) y
   la **Calidad** (Alta, Media o Baja). Por defecto se usa MP4 con calidad
   Media. La app muestra un ejemplo aproximado de tamaño para 10 minutos.
6. (Opcional) Pulsa **Guardar como...** para elegir en qué carpeta y con
   qué nombre se guardará el video. El diálogo siempre fuerza la
   extensión del formato elegido en el paso anterior, aunque escribas
   otra (por ejemplo, si el formato es MP4 y escribes `video.avi`, se
   guardará igual como `video.mp4`). Si no eliges nada, el video se
   guarda automáticamente en `recordings/` con un nombre basado en la
   fecha y hora.
7. (Opcional) Elige las horas, minutos y segundos en **Duración máx.**
   con las flechas o escribiendo los valores. Déjalo en `00:00:00` para
   detener la grabación manualmente. El tiempo en pausa no cuenta para este
   límite.
8. Pulsa **Iniciar grabación**. La ventana seleccionada se traerá al frente;
   si está activa la sincronización de Play/Pausa, se enviará Espacio y la
   captura comenzará enseguida. La vista previa y el cronómetro mostrarán
   lo que se está grabando y cuánto tiempo lleva. Windows o el reproductor
   pueden impedir la pulsación; en ese caso, inicia el video manualmente.
9. Usa **Pausar / Reanudar** para pausar temporalmente la captura y, si está
   activa la sincronización, pausar o reanudar también el video de la
   ventana. El cronómetro y el video grabado no incluyen el tiempo en pausa.
10. Pulsa **Detener** para finalizar manualmente, o espera a que se alcance
   la duración programada. Como el video ya se codificó en
   tiempo real mientras grababas, el archivo queda listo casi al instante
   (si grabaste audio, hay una mezcla rápida de un par de segundos como
   mucho). La app muestra la ruta del archivo final junto con su tamaño
   en MB.
11. Pulsa **Reproducir** para abrir el video con el reproductor
   predeterminado de Windows.

Si no usaste "Guardar como...", los videos se guardan en la carpeta
`recordings/` dentro del proyecto, con nombre
`recording_AAAAMMDD_HHMMSS.<extensión>` (según el formato elegido). Esa
carpeta está excluida del control de versiones (ver `.gitignore`).

### Formatos disponibles

| Formato | Códec de video/audio | Alta (1080p/30 fps) | Media (predeterminada, 15 fps) | Baja (15 fps) |
|---------|-----------------------|--------------------|-------------------------------|----------------|
| **MP4, MKV y MOV** | H.264 (`libx264`) + AAC 192 kb/s | CRF 16 · ~763 MiB | CRF 18 · ~436 MiB | CRF 23 · ~296 MiB |
| **AVI** | Xvid (`libxvid`) + MP3 192 kb/s | calidad 1 · ~2,924 MiB | calidad 2 · ~1,033 MiB | calidad 4 · ~647 MiB |
| **WEBM** | VP9 (`libvpx-vp9`) + Opus 128 kb/s | CRF 24 · ~860 MiB | CRF 28 · ~387 MiB | CRF 34 · ~298 MiB |

Los tamaños son ejemplos aproximados extrapolados a partir de una escena
sintética en movimiento a 1920×1080; los perfiles Alta se midieron a
30 fps y Media/Baja a 15 fps. Se extrapolaron clips cortos de prueba e
incluyen una pista de audio. El contenido real puede cambiar mucho el
resultado: una pantalla estática suele ocupar menos, y escenas con mucho
movimiento o detalle pueden ocupar más. Media/Baja usan la resolución
original de la ventana, por lo que su tamaño real también depende de ella.
Si la ventana es menor que 1080p, el perfil Alta la amplía, pero esto no
recupera detalles que no estaban en la fuente.
Los tres contenedores H.264 tienen tamaños similares; el resultado exacto
puede variar ligeramente.

Todos se codifican en vivo durante la grabación (no al final). En H.264 y
VP9, un CRF menor aumenta la calidad y el tamaño del archivo. En equipos
más lentos o con ventanas muy grandes, WEBM (VP9) puede no alcanzar a
codificar en tiempo real; los perfiles Alta también exigen más recursos
porque reescalan a 1080p y codifican 30 fps. Si la app se pone lenta,
prueba con MP4 o reduce la calidad.

## Generar un ejecutable (.exe)

Si prefieres no instalar Python, puedes empaquetar la app en un único
`.exe` para Windows con [PyInstaller](https://pyinstaller.org/):

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name GrabadorVentanas app.py
```

El ejecutable resultante queda en `dist/GrabadorVentanas.exe` y no requiere
tener Python ni las dependencias instaladas (todo, incluido `ffmpeg`,
queda embebido dentro del `.exe`). Las carpetas `build/`, `dist/` y el
archivo `.spec` generados por PyInstaller no se versionan (ver
`.gitignore`).

## Limitaciones importantes

- **Captura por región de pantalla, no por ventana real**: si otra
  ventana se superpone sobre la que estás grabando, se capturará lo que
  esté visible encima (limitación de las APIs de captura usadas). Evita
  tapar la ventana mientras grabas.
- **El audio es del sistema, no de la ventana específica**: Windows no
  ofrece una forma sencilla de capturar el audio de una sola
  ventana/pestaña sin usar APIs de bajo nivel más complejas (Process
  Loopback Capture). Lo que se graba es *todo* el audio que sale por el
  dispositivo de salida predeterminado. Si necesitas que el audio
  corresponda solo al navegador, evita reproducir otros sonidos mientras
  grabas.
- Solo funciona en **Windows**, por las librerías utilizadas
  (`pywin32`, `pygetwindow`, WASAPI).
- Si `PyAudioWPatch` no se pudo instalar o inicializar, la app sigue
  funcionando pero solo grabará video (el checkbox de audio aparece
  deshabilitado y se muestra un aviso en la interfaz).
- Si `pycaw` no se pudo instalar o inicializar, la barra de volumen
  aparece deshabilitada y se muestra un aviso; el resto de la app sigue
  funcionando con normalidad.
- **La app no automatiza inicios de sesión**: el campo "Abrir video (URL)"
  solo abre el link en tu navegador (`webbrowser.open`); no guarda,
  escribe ni gestiona usuarios o contraseñas de ningún servicio (Google
  Drive, Teams, etc.). Cualquier inicio de sesión lo haces tú manualmente
  en la pestaña que se abre.

## Estructura del proyecto

```
appvideo/
├── app.py              # Aplicación principal (interfaz + lógica de grabación)
├── audio.py            # Captura de audio loopback
├── config.py           # Formatos, perfiles y ajustes de captura
├── drive.py            # Importación de videos desde Google Drive público
├── media.py            # Mezcla de audio y video
├── requirements.txt    # Dependencias de Python
├── .gitignore
├── README.md
└── recordings/         # Videos generados (no versionado)
```

## Cómo funciona internamente (resumen técnico)

1. Al iniciar la grabación, se obtiene el rectángulo de la ventana
   (`win32gui.GetWindowRect`, ajustado a dimensiones pares) y se lanza un
   hilo que captura esa región de pantalla cuadro a cuadro con `mss`.
2. Cada cuadro se envía por un pipe (`stdin`) a un proceso `ffmpeg` (vía
   `imageio-ffmpeg`) que lo codifica **en tiempo real** ya con el códec
   final del formato elegido (por ejemplo H.264 con CRF `18` y preset
   `veryfast` para MP4/MKV/MOV), escribiendo directamente el archivo de video
   temporal (`.tmp_video_*.<ext>`) — no hay una recodificación posterior
   del video.
3. Si el audio está activado, un segundo hilo abre un stream WASAPI en
   modo *loopback* con `PyAudioWPatch` y guarda el audio capturado en un
   `.wav` temporal (`.tmp_audio_*.wav`).
4. Cada cuadro capturado se comparte con la interfaz mediante una
   variable protegida por un `Lock`, y un bucle periódico de Tkinter
   (`root.after`) la usa para actualizar la vista previa y el cronómetro
   sin bloquear la grabación.
5. Al pausar, ambos hilos siguen vivos pero dejan de enviar datos al pipe
   de ffmpeg (video) o de acumular datos (audio); el cronómetro también
   se congela. Nada de eso queda en el resultado final.
6. Al detener, se cierra el pipe de ffmpeg (que termina de escribir el
   archivo de video, ya listo) y se espera a que ambos hilos terminen.
   Si no hubo audio, el video temporal se mueve directamente a
   `recordings/` (o a la ruta elegida) sin más procesamiento. Si hubo
   audio, se hace una mezcla rápida con `ffmpeg` (`-c:v copy`, solo
   codifica el audio) para combinarlo con el video ya codificado. Los
   archivos temporales se eliminan al terminar.
