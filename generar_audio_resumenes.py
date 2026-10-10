#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generar_audio_resumenes.py
===========================

Convierte los resúmenes de estudio (.txt) a audio MP3 usando las voces
neuronales gratuitas de Microsoft Edge (el mismo motor que usa la función
"Leer en voz alta" del navegador Edge), a través de la librería de código
abierto `edge-tts`. No requiere cuenta, API key, ni pago — y no tiene los
límites de caracteres de las páginas web (TTSFree, ttsmp3.com, etc.).

Además del .mp3, genera un archivo .json con la marca de tiempo exacta
(inicio_ms, fin_ms) de cada palabra, para poder resaltar el texto en
sincronía con el audio estilo "letras de Spotify" (ver karaoke-apuntes.html).

IMPORTANTE: este script se ejecuta en TU computadora, no en la sesión de
Claude. Necesita conexión normal a internet (la de tu PC), Python 3.8+ y el
paquete `edge-tts` instalado.

INSTALACIÓN (una sola vez):
    pip install edge-tts

USO:
    # Uso simple: convierte TODOS los resumen-*.txt de esta carpeta usando
    # la voz elegida (es-CO-SalomeNeural, Colombia, femenina) por defecto,
    # generando su .mp3 y su .json de sincronización.
    python generar_audio_resumenes.py

    # Ver la lista completa de voces en español disponibles ahora mismo
    # (la lista la trae Microsoft en vivo, así que siempre está actualizada)
    python generar_audio_resumenes.py --listar-voces

    # Generar un audio de PRUEBA corto con varias voces sugeridas, para
    # escuchar y elegir otra voz distinta si quieres comparar
    python generar_audio_resumenes.py --previsualizar

    # Convertir UN solo archivo puntual con otra voz
    python generar_audio_resumenes.py --archivo resumen-base-de-datos.txt --voz es-BO-MarceloNeural

    # Convertir todos con otra voz distinta a la de por defecto
    python generar_audio_resumenes.py --todos --voz es-ES-AlvaroNeural

    # Opcional: cambiar la velocidad de lectura (por defecto normal)
    python generar_audio_resumenes.py --velocidad +15%
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

try:
    import edge_tts
except ImportError:
    print("Falta el paquete 'edge-tts'. Instálalo con:\n\n    pip install edge-tts\n")
    sys.exit(1)


# Un puñado de voces sugeridas para la previsualización rápida (varios
# acentos/géneros). La lista completa siempre se puede ver con --listar-voces.
VOCES_SUGERIDAS = [
    "es-CO-SalomeNeural",    # Colombia, femenina (la elegida)
    "es-BO-MarceloNeural",   # Bolivia, masculina
    "es-BO-SofiaNeural",     # Bolivia, femenina
    "es-ES-AlvaroNeural",    # España, masculina
    "es-ES-ElviraNeural",    # España, femenina
    "es-MX-JorgeNeural",     # México, masculina
    "es-MX-DaliaNeural",     # México, femenina
    "es-AR-TomasNeural",     # Argentina, masculina
    "es-US-AlonsoNeural",    # Latino neutro (EE.UU.), masculina
]

TEXTO_PREVIEW = (
    "Hola, esta es una prueba de voz para escuchar los resúmenes de estudio "
    "del plan de titulación. Si esta voz te gusta, úsala para convertir el "
    "resumen completo."
)

# Los offsets que entrega edge-tts vienen en "ticks" de 100 nanosegundos.
# Dividir entre 10000 los convierte a milisegundos.
TICKS_POR_MS = 10_000


def _mensaje_error_conexion(e):
    return (
        f"  (No se pudo generar el audio: {e}\n"
        f"   Revisa tu conexión a internet, o que el nombre de la voz "
        f"sea correcto con --listar-voces.)"
    )


async def listar_voces_es():
    try:
        voces = await edge_tts.list_voices()
    except Exception as e:
        print(f"No se pudo obtener la lista de voces (¿hay conexión a internet?): {e}")
        return
    voces_es = sorted(
        [v for v in voces if v["Locale"].startswith("es-")],
        key=lambda v: v["ShortName"],
    )
    print(f"\nVoces en español disponibles ahora mismo ({len(voces_es)} en total):\n")
    for v in voces_es:
        genero = "F" if v["Gender"] == "Female" else "M"
        print(f"  {v['ShortName']:<28} [{genero}]  {v['Locale']}")
    print()


async def generar_audio_simple(texto, voz, salida, velocidad="+0%"):
    """Genera solo el .mp3, sin marcas de tiempo (usado en la previsualización)."""
    comunicador = edge_tts.Communicate(texto, voz, rate=velocidad)
    await comunicador.save(str(salida))
    print(f"  -> Generado: {salida}")


async def generar_audio_con_marcas(
    texto, voz, salida_mp3, salida_json, velocidad="+0%", al_progresar=None
):
    """
    Genera el .mp3 y, en la misma pasada, el .json con el inicio_ms/fin_ms
    de cada palabra, usando boundary="WordBoundary" + .stream().

    al_progresar(palabras_listas, palabras_estimadas) se llama por cada palabra
    (lo usa el servidor de la página web para mostrar una barra de progreso).
    """
    comunicador = edge_tts.Communicate(texto, voz, rate=velocidad, boundary="WordBoundary")
    total_estimado = max(len(texto.split()), 1)

    palabras = []
    with open(salida_mp3, "wb") as archivo_audio:
        async for trozo in comunicador.stream():
            if trozo["type"] == "audio":
                archivo_audio.write(trozo["data"])
            elif trozo["type"] == "WordBoundary":
                inicio_ms = trozo["offset"] / TICKS_POR_MS
                fin_ms = (trozo["offset"] + trozo["duration"]) / TICKS_POR_MS
                palabras.append(
                    {
                        "texto": trozo["text"],
                        "inicio_ms": round(inicio_ms),
                        "fin_ms": round(fin_ms),
                    }
                )
                if al_progresar:
                    al_progresar(len(palabras), total_estimado)

    datos = {"voz": voz, "velocidad": velocidad, "palabras": palabras}
    with open(salida_json, "w", encoding="utf-8") as archivo_json:
        json.dump(datos, archivo_json, ensure_ascii=False, indent=2)

    print(f"  -> Generado: {salida_mp3}")
    print(f"  -> Generado: {salida_json}  ({len(palabras)} palabras sincronizadas)")


async def previsualizar():
    carpeta = Path("previsualizacion_voces")
    carpeta.mkdir(exist_ok=True)
    print(f"Generando muestras cortas en la carpeta '{carpeta}/' ...\n")
    for voz in VOCES_SUGERIDAS:
        salida = carpeta / f"muestra_{voz}.mp3"
        try:
            await generar_audio_simple(TEXTO_PREVIEW, voz, salida)
        except Exception as e:
            print(f"  (no se pudo generar {voz}: {e})")
    print(
        "\nListo. Escucha los archivos en la carpeta 'previsualizacion_voces' "
        "y anota el nombre de la voz que más te guste (ej. es-BO-MarceloNeural) "
        "para usarla con --voz al convertir los resúmenes completos."
    )


async def convertir_archivo(ruta_txt, voz, velocidad):
    ruta_txt = Path(ruta_txt)
    if not ruta_txt.exists():
        print(f"No se encontró el archivo: {ruta_txt}")
        return
    texto = ruta_txt.read_text(encoding="utf-8").strip()
    if not texto:
        print(f"  (El archivo '{ruta_txt.name}' está vacío, se omite.)")
        return
    salida_mp3 = ruta_txt.with_suffix(".mp3")
    salida_json = ruta_txt.with_suffix(".json")
    print(f"Convirtiendo '{ruta_txt.name}' con la voz {voz} ...")
    try:
        await generar_audio_con_marcas(texto, voz, salida_mp3, salida_json, velocidad)
    except Exception as e:
        print(_mensaje_error_conexion(e))


async def convertir_todos(voz, velocidad):
    archivos = sorted(Path(".").glob("resumen-*.txt"))
    if not archivos:
        print(
            "No se encontraron archivos 'resumen-*.txt' en esta carpeta. "
            "Coloca este script en la misma carpeta que los resúmenes, o "
            "usa --archivo para indicar la ruta exacta."
        )
        return
    print(f"Se encontraron {len(archivos)} archivo(s). Usando la voz: {voz}\n")
    for ruta in archivos:
        await convertir_archivo(ruta, voz, velocidad)


def main():
    parser = argparse.ArgumentParser(
        description="Convierte los resúmenes .txt a audio MP3 (con .json de sincronización) usando voces neuronales gratuitas."
    )
    parser.add_argument("--listar-voces", action="store_true", help="Muestra todas las voces en español disponibles.")
    parser.add_argument("--previsualizar", action="store_true", help="Genera muestras cortas de varias voces sugeridas.")
    parser.add_argument("--archivo", type=str, help="Ruta a un archivo .txt específico a convertir.")
    parser.add_argument("--todos", action="store_true", help="Convierte todos los resumen-*.txt de esta carpeta.")
    parser.add_argument("--voz", type=str, default="es-CO-SalomeNeural", help="Nombre de la voz (ver --listar-voces). Por defecto: es-CO-SalomeNeural (Colombia, femenina).")
    parser.add_argument("--velocidad", type=str, default="+0%", help="Ajuste de velocidad, ej. +15%% o -10%%.")
    args = parser.parse_args()

    try:
        if args.listar_voces:
            asyncio.run(listar_voces_es())
        elif args.previsualizar:
            asyncio.run(previsualizar())
        elif args.archivo:
            asyncio.run(convertir_archivo(args.archivo, args.voz, args.velocidad))
        else:
            # Por defecto (sin flags, o con --todos): convierte todos los
            # resumen-*.txt de la carpeta actual con la voz elegida (Salome).
            asyncio.run(convertir_todos(args.voz, args.velocidad))
    except KeyboardInterrupt:
        print("\nCancelado por el usuario.")
    except Exception as e:
        print(
            f"\nOcurrió un error inesperado: {e}\n"
            "Revisa tu conexión a internet e inténtalo de nuevo."
        )


if __name__ == "__main__":
    main()
