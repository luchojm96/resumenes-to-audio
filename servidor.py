#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
servidor.py
============

Servidor local (solo 127.0.0.1) que permite convertir apuntes a audio desde la
página Angular. El navegador no puede ejecutar Python ni edge-tts, así que la
página le pide el trabajo a este servidor, que usa conversor.py.

    python servidor.py              # puerto 8765
    python servidor.py --puerto 9000

Los resultados también quedan guardados en la carpeta ./salida/<nombre>/.

API (la página Angular la consume a través del proxy /api):
    GET  /api/salud                         estado del servidor
    GET  /api/voces                         voces en español disponibles
    POST /api/convertir                     archivo + voz + velocidad -> id de trabajo
    GET  /api/trabajos/{id}                 progreso del trabajo
    GET  /api/trabajos/{id}/archivos/{n}    descarga un archivo generado
"""

import argparse
import asyncio
import re
import uuid
from pathlib import Path

from aiohttp import web

import conversor
from generar_audio_resumenes import VOCES_SUGERIDAS

RAIZ = Path(__file__).parent
CARPETA_SALIDA = RAIZ / "salida"
VOZ_POR_DEFECTO = "es-CO-SalomeNeural"
LIMITE_SUBIDA = 30 * 1024 * 1024

RE_VOZ = re.compile(r"^[a-z]{2,3}-[A-Z]{2}-[A-Za-z]+Neural$")
RE_VELOCIDAD = re.compile(r"^[+-]\d{1,3}%$")

trabajos: dict[str, dict] = {}
tareas: set[asyncio.Task] = set()
cache_voces: list[dict] | None = None


def error(mensaje: str, estado: int = 400) -> web.Response:
    return web.json_response({"error": mensaje}, status=estado)


async def salud(request):
    try:
        import pypdf  # noqa: F401
        pdf = True
    except ImportError:
        pdf = False
    return web.json_response({"ok": True, "pdf": pdf})


async def voces(request):
    global cache_voces
    if cache_voces is None:
        try:
            import edge_tts

            todas = await edge_tts.list_voices()
            cache_voces = [
                {
                    "id": v["ShortName"],
                    "nombre": v["ShortName"].split("-", 2)[2].replace("Neural", ""),
                    "region": v["Locale"],
                    "genero": "Femenina" if v["Gender"] == "Female" else "Masculina",
                }
                for v in sorted(todas, key=lambda v: v["ShortName"])
                if v["Locale"].startswith("es-")
            ]
        except Exception:
            # Sin internet no se puede pedir la lista en vivo: se ofrecen las sugeridas.
            return web.json_response(
                [
                    {"id": v, "nombre": v.split("-", 2)[2].replace("Neural", ""), "region": v[:5], "genero": ""}
                    for v in VOCES_SUGERIDAS
                ]
            )
    return web.json_response(cache_voces)


async def crear_trabajo(request):
    # Un origen ajeno no puede enviar este encabezado sin pasar por CORS, que este
    # servidor no habilita: así otras webs abiertas en el navegador no pueden
    # disparar conversiones en tu equipo.
    if request.headers.get("X-Karaoke") != "1":
        return error("Solicitud no permitida.", 403)

    formulario = await request.post()
    archivo = formulario.get("archivo")
    if archivo is None or not hasattr(archivo, "file"):
        return error("Falta el archivo a convertir.")
    voz = formulario.get("voz") or VOZ_POR_DEFECTO
    velocidad = formulario.get("velocidad") or "+0%"
    if not RE_VOZ.match(str(voz)):
        return error("Nombre de voz inválido.")
    if not RE_VELOCIDAD.match(str(velocidad)):
        return error("Velocidad inválida: usa un formato como +15% o -10%.")
    if Path(archivo.filename).suffix.lower() not in conversor.EXTENSIONES_PERMITIDAS:
        return error("Formato no soportado. Usa: .md, .pdf, .json o .txt.")

    datos = archivo.file.read()
    if not datos:
        return error("El archivo está vacío.")

    id_trabajo = uuid.uuid4().hex[:12]
    trabajo = {
        "estado": "en_curso",
        "etapa": "preparando",
        "progreso": 0.0,
        "mensaje": "En cola...",
        "archivo": archivo.filename,
        "error": None,
        "archivos": {},
    }
    trabajos[id_trabajo] = trabajo

    def al_estado(etapa, progreso, mensaje):
        trabajo.update(etapa=etapa, progreso=progreso, mensaje=mensaje)

    async def correr():
        try:
            trabajo["archivos"] = await conversor.convertir(
                archivo.filename, datos, str(voz), str(velocidad), CARPETA_SALIDA, al_estado
            )
            trabajo["estado"] = "listo"
        except conversor.ErrorConversion as e:
            trabajo.update(estado="error", error=str(e))
        except Exception as e:  # noqa: BLE001
            trabajo.update(estado="error", error=f"Error inesperado: {e}")

    tarea = asyncio.create_task(correr())
    tareas.add(tarea)
    tarea.add_done_callback(tareas.discard)
    return web.json_response({"id": id_trabajo})


def _trabajo(request):
    return trabajos.get(request.match_info["id"])


async def estado_trabajo(request):
    t = _trabajo(request)
    if t is None:
        return error("Trabajo no encontrado.", 404)
    return web.json_response(
        {
            "estado": t["estado"],
            "etapa": t["etapa"],
            "progreso": t["progreso"],
            "mensaje": t["mensaje"],
            "archivo": t["archivo"],
            "error": t["error"],
            "archivos": list(t["archivos"].keys()),
        }
    )


async def descargar_archivo(request):
    t = _trabajo(request)
    if t is None:
        return error("Trabajo no encontrado.", 404)
    ruta = t["archivos"].get(request.match_info["nombre"])  # solo nombres generados por este trabajo
    if ruta is None or not Path(ruta).exists():
        return error("Archivo no encontrado.", 404)
    return web.FileResponse(ruta)


def crear_app() -> web.Application:
    app = web.Application(client_max_size=LIMITE_SUBIDA)
    app.add_routes(
        [
            web.get("/api/salud", salud),
            web.get("/api/voces", voces),
            web.post("/api/convertir", crear_trabajo),
            web.get("/api/trabajos/{id}", estado_trabajo),
            web.get("/api/trabajos/{id}/archivos/{nombre}", descargar_archivo),
        ]
    )
    return app


def main():
    parser = argparse.ArgumentParser(description="Servidor local para convertir apuntes a audio.")
    parser.add_argument("--puerto", type=int, default=8765)
    args = parser.parse_args()
    CARPETA_SALIDA.mkdir(exist_ok=True)
    print(f"Servidor de conversión en http://127.0.0.1:{args.puerto}  (Ctrl+C para detener)")
    web.run_app(crear_app(), host="127.0.0.1", port=args.puerto, print=None)


if __name__ == "__main__":
    main()
