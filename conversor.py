#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
conversor.py
=============

Convierte apuntes (.md, .pdf, .json o .txt) en los tres archivos que usa el
reproductor karaoke:

    <nombre>.mp3               audio con la voz neuronal elegida
    <nombre>.json              marca de tiempo de cada palabra (sincronización)
    <nombre>.estructura.json   formato visual (títulos, negritas, listas...)

Flujo: cualquier formato de entrada -> markdown -> texto narrable -> TTS
(generar_audio_resumenes.py) -> alineación palabra por palabra del markdown
contra lo narrado (herramientas/build_bloques.py).

Lo usa servidor.py (la página web), pero también se puede importar solo.
"""

import asyncio
import io
import json
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "herramientas"))

from generar_audio_resumenes import generar_audio_con_marcas  # noqa: E402
from align_md_txt import RE_CALLOUT, RE_HR, RE_TAREA, texto_plano  # noqa: E402
from build_bloques import construir  # noqa: E402

EXTENSIONES_PERMITIDAS = {".md", ".markdown", ".txt", ".pdf", ".json"}


class ErrorConversion(Exception):
    """Error con un mensaje pensado para mostrarse tal cual al usuario."""


# ---------------------------------------------------------------- entrada -> markdown


def json_a_markdown(datos: dict) -> str:
    if not isinstance(datos, dict):
        raise ErrorConversion('El .json debe ser un objeto: {"titulo": ..., "secciones": [...]}.')

    def texto(valor, campo):
        if valor is None:
            return None
        if not isinstance(valor, str):
            raise ErrorConversion(f'El campo "{campo}" debe ser texto.')
        return valor.strip() or None

    def lista_de_textos(valor, campo):
        if valor is None:
            return []
        if not isinstance(valor, list) or not all(isinstance(x, str) for x in valor):
            raise ErrorConversion(f'El campo "{campo}" debe ser una lista de textos.')
        return [x.strip() for x in valor if x.strip()]

    partes = []
    titulo = texto(datos.get("titulo"), "titulo")
    if titulo:
        partes.append(f"# {titulo}")
    intro = texto(datos.get("intro"), "intro")
    if intro:
        partes.append(intro)

    secciones = datos.get("secciones")
    cuerpo = texto(datos.get("texto"), "texto")
    if secciones is None and not cuerpo:
        raise ErrorConversion('Falta el campo "secciones" (o "texto") en el .json.')
    if cuerpo:
        partes.append(cuerpo)
    if secciones is not None:
        if not isinstance(secciones, list):
            raise ErrorConversion('El campo "secciones" debe ser una lista.')
        for i, sec in enumerate(secciones, start=1):
            if not isinstance(sec, dict):
                raise ErrorConversion(f'La sección {i} debe ser un objeto con "titulo", "parrafos", etc.')
            t = texto(sec.get("titulo"), f"secciones[{i}].titulo")
            if t:
                partes.append(f"## {t}")
            partes.extend(lista_de_textos(sec.get("parrafos"), f"secciones[{i}].parrafos"))
            items = lista_de_textos(sec.get("lista"), f"secciones[{i}].lista")
            if items:
                partes.append("\n".join(f"- {x}" for x in items))
            pasos = lista_de_textos(sec.get("pasos"), f"secciones[{i}].pasos")
            if pasos:
                partes.append("\n".join(f"{n}. {x}" for n, x in enumerate(pasos, start=1)))
    return "\n\n".join(partes)


def _lineas_con_formato(lector):
    """
    Devuelve [(pagina, y, tamano, texto)] usando la posición y el tamaño de letra
    de cada fragmento de texto del PDF (permite detectar títulos y párrafos).
    """
    import math

    lineas = []
    for n_pagina, pagina in enumerate(lector.pages):
        fragmentos = []

        def visitante(texto, cm, tm, fuente, tamano):
            if texto and texto.strip():
                y = tm[5] * cm[3] + cm[5]
                efectivo = (tamano or 1) * math.hypot(tm[2], tm[3]) * math.hypot(cm[2], cm[3])
                fragmentos.append((y, efectivo, texto))

        pagina.extract_text(visitor_text=visitante)
        for y, tam, texto in fragmentos:
            if lineas and lineas[-1][0] == n_pagina and abs(lineas[-1][1] - y) < 0.3 * max(tam, 1):
                pg, y0, t0, previo = lineas[-1]
                lineas[-1] = (pg, y0, max(t0, tam), previo + texto)
            else:
                lineas.append((n_pagina, y, tam, texto))
    return [(pg, y, tam, t.strip()) for pg, y, tam, t in lineas if t.strip()]


def _markdown_por_posicion(lineas) -> str:
    from collections import Counter

    pesos = Counter()
    for _, _, tam, texto in lineas:
        pesos[round(tam * 2) / 2] += len(texto)
    cuerpo = pesos.most_common(1)[0][0]

    bloques, actual, previa = [], "", None
    for pg, y, tam, texto in lineas:
        es_titulo = tam >= cuerpo * 1.15 and len(texto) < 120
        if es_titulo:
            if actual:
                bloques.append(actual)
                actual = ""
            bloques.append(("# " if tam >= cuerpo * 1.5 else "## ") + texto)
            previa = None
            continue

        if previa is not None:
            mismo_pg, y_prev, tam_prev = previa
            if mismo_pg == pg:
                nuevo_parrafo = (y_prev - y) > 1.6 * max(tam, tam_prev)
            else:
                nuevo_parrafo = actual[-1:] in (".", ":", "!", "?")
            if nuevo_parrafo and actual:
                bloques.append(actual)
                actual = ""
        if actual.endswith("-") and texto[:1].islower():
            actual = actual[:-1] + texto
        else:
            actual = f"{actual} {texto}".strip()
        previa = (pg, y, tam)
    if actual:
        bloques.append(actual)
    return "\n\n".join(bloques)


def _markdown_por_longitud(paginas) -> str:
    """Respaldo cuando el PDF no entrega posiciones: reconstruye párrafos por la longitud de línea."""
    lineas = [l.strip() for pagina in paginas for l in pagina.splitlines()]
    largos = sorted(len(l) for l in lineas if l)
    mediana = largos[len(largos) // 2]
    parrafos, actual = [], ""
    for linea in lineas:
        if not linea:
            if actual:
                parrafos.append(actual)
                actual = ""
            continue
        if actual.endswith("-") and linea[:1].islower():
            actual = actual[:-1] + linea
        else:
            actual = f"{actual} {linea}".strip()
        if len(linea) < 0.7 * mediana and actual[-1] in ".:!?":
            parrafos.append(actual)
            actual = ""
    if actual:
        parrafos.append(actual)
    return "\n\n".join(parrafos)


def pdf_a_markdown(datos: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ErrorConversion("Para leer PDFs falta el paquete 'pypdf'. Instálalo con: pip install pypdf")

    try:
        lector = PdfReader(io.BytesIO(datos))
        if lector.is_encrypted:
            raise ErrorConversion("El PDF está protegido con contraseña; quítale la protección e inténtalo de nuevo.")
        paginas = [(p.extract_text() or "") for p in lector.pages]
    except ErrorConversion:
        raise
    except Exception as e:
        raise ErrorConversion(f"No se pudo leer el PDF: {e}")

    if not any(p.strip() for p in paginas):
        raise ErrorConversion("El PDF no tiene texto seleccionable (¿es un escaneo/imagen?). Usa uno con texto real.")

    try:
        lineas = _lineas_con_formato(lector)
        if lineas:
            return _markdown_por_posicion(lineas)
    except Exception:
        pass
    return _markdown_por_longitud(paginas)


def a_markdown(nombre_archivo: str, datos: bytes) -> str:
    ext = Path(nombre_archivo).suffix.lower()
    if ext not in EXTENSIONES_PERMITIDAS:
        raise ErrorConversion(
            f"Formato '{ext or 'desconocido'}' no soportado. Usa: .md, .pdf, .json o .txt."
        )
    if ext == ".pdf":
        return pdf_a_markdown(datos)
    try:
        texto = datos.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ErrorConversion("El archivo no está en UTF-8. Guárdalo con codificación UTF-8 e inténtalo de nuevo.")
    if ext == ".json":
        try:
            return json_a_markdown(json.loads(texto))
        except json.JSONDecodeError as e:
            raise ErrorConversion(f"El .json no es válido (línea {e.lineno}, columna {e.colno}): {e.msg}.")
    return texto


# ---------------------------------------------------------------- markdown -> narración


def _limpiar_inline(t: str) -> str:
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", t)
    t = re.sub(r"\[\[([^\]]+)\]\]", r"\1", t)
    return texto_plano(t).strip()


def _con_punto(t: str) -> str:
    return t if not t or t[-1] in ".:;!?" else t + "."


def md_a_narracion(md: str) -> str:
    """
    Texto para el TTS: sin marcas de markdown, sin bloques de código (no se
    pueden leer en voz alta) y con las tablas dichas como oraciones
    ("Encabezado: valor.") porque una tabla no se puede locutar tal cual.
    """
    if md.startswith("---"):
        cierre = md.find("\n---", 3)
        if cierre != -1:
            md = md[cierre + 4 :]

    lineas = md.split("\n")
    salida = []
    i = 0
    while i < len(lineas):
        linea = lineas[i].strip()
        if linea.startswith("```"):
            i += 1
            while i < len(lineas) and not lineas[i].strip().startswith("```"):
                i += 1
            i += 1
            continue
        if not linea or linea.startswith("<!--") or RE_HR.match(linea):
            i += 1
            continue
        if linea.startswith(">"):
            cita = _limpiar_inline(RE_CALLOUT.sub("", re.sub(r"^(>\s?)+", "", linea)))
            if cita:
                salida.append(_con_punto(cita))
            i += 1
            continue
        if linea.startswith("|"):
            filas = []
            while i < len(lineas) and lineas[i].strip().startswith("|"):
                filas.append(lineas[i].strip())
                i += 1
            filas = [f for f in filas if not re.match(r"^\|[\s:|-]+\|$", f)]
            if filas:
                encabezado = [_limpiar_inline(c) for c in filas[0].strip("|").split("|")]
                for fila in filas[1:]:
                    celdas = [_limpiar_inline(c) for c in fila.strip("|").split("|")]
                    salida.append(
                        " ".join(
                            _con_punto(f"{encabezado[k]}: {c}" if k < len(encabezado) else c)
                            for k, c in enumerate(celdas)
                        )
                    )
            continue

        m = (
            re.match(r"^#{1,6}\s+(.*)$", linea)
            or re.match(r"^[-*+]\s+(.*)$", linea)
            or re.match(r"^\d+[.)]\s+(.*)$", linea)
        )
        texto = _limpiar_inline(RE_TAREA.sub("", m.group(1)) if m else linea)
        if texto:
            salida.append(_con_punto(texto) if m else texto)
        i += 1
    return "\n".join(salida)


# ---------------------------------------------------------------- pipeline completo


def nombre_base_seguro(nombre_archivo: str) -> str:
    base = re.sub(r"[^\w.-]+", "-", Path(nombre_archivo).stem, flags=re.UNICODE).strip("-.")
    return base or "apuntes"


async def convertir(nombre_archivo, datos, voz, velocidad, carpeta_salida, al_estado):
    """
    Devuelve {nombre_de_archivo: Path}. al_estado(etapa, progreso 0..1, mensaje).
    Lanza ErrorConversion con un mensaje legible si algo falla.
    """
    al_estado("preparando", 0.0, "Leyendo y preparando el texto...")
    md = a_markdown(nombre_archivo, datos)
    narracion = md_a_narracion(md)
    if not narracion.strip():
        raise ErrorConversion("El archivo no tiene texto que narrar.")

    base = nombre_base_seguro(nombre_archivo)
    carpeta = Path(carpeta_salida) / base
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta_md = carpeta / f"{base}.fuente.md"
    ruta_txt = carpeta / f"{base}.txt"
    ruta_mp3 = carpeta / f"{base}.mp3"
    ruta_json = carpeta / f"{base}.json"
    ruta_estructura = carpeta / f"{base}.estructura.json"
    ruta_md.write_text(md, encoding="utf-8")
    ruta_txt.write_text(narracion, encoding="utf-8")

    palabras_estimadas = len(narracion.split())
    al_estado("sintetizando", 0.0, f"Generando audio (~{palabras_estimadas} palabras)...")

    def progreso(hechas, total):
        al_estado("sintetizando", min(hechas / total, 0.99), f"Generando audio: {hechas} de ~{total} palabras")

    try:
        await generar_audio_con_marcas(narracion, voz, ruta_mp3, ruta_json, velocidad, al_progresar=progreso)
    except ErrorConversion:
        raise
    except Exception as e:
        raise ErrorConversion(
            "No se pudo generar el audio. Revisa tu conexión a internet y que la voz "
            f"'{voz}' exista. Detalle: {e}"
        )

    archivos = {ruta_mp3.name: ruta_mp3, ruta_json.name: ruta_json, ruta_txt.name: ruta_txt}

    al_estado("estructurando", 0.99, "Armando el formato visual (títulos, listas, negritas)...")
    try:
        estructura = await asyncio.to_thread(construir, ruta_md, ruta_json)
        ruta_estructura.write_text(json.dumps(estructura, ensure_ascii=False, indent=2), encoding="utf-8")
        archivos[ruta_estructura.name] = ruta_estructura
    except Exception:
        # La estructura es opcional: sin ella el reproductor muestra texto plano.
        pass

    al_estado("listo", 1.0, "Conversión terminada.")
    return archivos
