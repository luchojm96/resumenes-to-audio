#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_bloques.py
=================

Genera "resumen-X.estructura.json": una descripción de bloques (títulos,
párrafos, listas, filas de tabla...) con formato (negrita/código/etiqueta),
donde cada bloque referencia un RANGO DE ÍNDICES [inicio, fin) dentro del
mismo array "palabras" que ya usa resumen-X.json para la sincronización de
audio. No se duplica texto: el texto real que se muestra siempre sale del
.json de sincronización (que es la fuente de verdad de lo que efectivamente
se escucha); este archivo solo aporta el "cómo se ve".

Estrategia (ver tools/align_md_txt.py para el analisis que motivó esto):
 - Las palabras que coinciden entre el .md y el .txt narrado heredan el
   formato exacto del markdown (negrita, encabezado, viñeta, etc.).
 - Las palabras narradas que NO tienen equivalente en el .md (el párrafo
   introductorio agregado solo para TTS, alguna reformulación puntual de una
   frase con código/fórmulas) se asignan al bloque vecino más cercano en el
   propio .md, sin formato especial (texto plano dentro de ese bloque) --
   nunca se pierden ni se dejan sin bloque: solo no llevan negrita/etc.
 - El párrafo introductorio al inicio del archivo (siempre el primer hueco,
   antes de la palabra 0 del .md) es la única excepción: se marca como
   bloque "caption" aparte, para no fundirlo con el título.
 - El contenido del .md que nunca se narra (bloques de código, fragmentos
   de fórmulas/sintaxis que el .txt omitió) simplemente no genera ningún
   bloque: no hay nada que resaltar porque no hay audio para esas palabras.

Uso:
    python herramientas/build_bloques.py <archivo.md> <archivo.json> <salida.json>
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from align_md_txt import tokenizar_md, normalizar_palabra, cargar_palabras_json  # noqa: E402
from difflib import SequenceMatcher


def es_trivial(md_frag: str, json_frag: str) -> bool:
    a = re.sub(r"[^a-zà-öø-ÿ0-9]", "", md_frag.lower())
    b = re.sub(r"[^a-zà-öø-ÿ0-9]", "", json_frag.lower())
    return a == b


def construir(md_path: Path, json_path: Path):
    md_text = md_path.read_text(encoding="utf-8")
    tokens, bloques_md = tokenizar_md(md_text)
    md_words = [normalizar_palabra(t.texto) for t in tokens]

    json_words_raw = cargar_palabras_json(json_path)
    json_words = [normalizar_palabra(w) for w in json_words_raw]
    n = len(json_words_raw)

    sm = SequenceMatcher(a=md_words, b=json_words, autojunk=False)
    opcodes = sm.get_opcodes()

    # asignacion[j] = (bloque_id_o_None_para_caption, formato_tuple)
    CAPTION = "__caption__"
    asignacion = [None] * n

    def bloque_vecino(i1):
        """Bloque md más cercano para una palabra hablada sin equivalente en el md."""
        if i1 <= 0:
            return CAPTION if i1 == 0 and not tokens else (tokens[0].bloque_id if tokens else CAPTION)
        if i1 >= len(tokens):
            return tokens[-1].bloque_id if tokens else CAPTION
        return tokens[i1].bloque_id

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            for k in range(j2 - j1):
                tok = tokens[i1 + k]
                asignacion[j1 + k] = (tok.bloque_id, tok.formatos)
            continue

        if tag == "delete":
            continue  # texto del md que nunca se pronunció (código, fórmulas): no genera bloque

        # replace / insert
        md_fragment = " ".join(t.texto for t in tokens[i1:i2])
        json_fragment = " ".join(json_words_raw[j1:j2])
        if tag == "replace" and (i2 - i1) == (j2 - j1) and es_trivial(md_fragment, json_fragment):
            for k in range(j2 - j1):
                tok = tokens[i1 + k]
                asignacion[j1 + k] = (tok.bloque_id, tok.formatos)
            continue

        # Caso especial: el primer hueco del archivo es el párrafo introductorio
        # agregado solo para TTS (nunca existió en el .md original).
        if i1 == 0 and j1 == 0:
            bid = CAPTION
        else:
            bid = bloque_vecino(i1)
        for k in range(j2 - j1):
            asignacion[j1 + k] = (bid, ())

    # Verificación de integridad: cada palabra hablada debe tener bloque asignado.
    faltantes = [j for j, a in enumerate(asignacion) if a is None]
    if faltantes:
        raise RuntimeError(f"Palabras sin bloque asignado (bug en el alineador): {faltantes[:20]}")

    # Agrupa índices consecutivos con el mismo bloque en bloques de salida,
    # y dentro de cada uno, agrupa "runs" de formato no vacío.
    bloques_out = []

    # Bloques que nunca se narran (código, líneas horizontales) no tienen palabras
    # que resaltar, pero se conservan como bloques vacíos (inicio == fin) en su
    # posición para que el reproductor pueda mostrarlos como un visor de markdown.
    estaticos = [b for b in bloques_md if b.tipo in ("code", "hr")]

    def volcar_estaticos(hasta_id, posicion):
        while estaticos and estaticos[0].id < hasta_id:
            e = estaticos.pop(0)
            bloques_out.append({"tipo": e.tipo, "inicio": posicion, "fin": posicion, **e.meta})

    j = 0
    while j < n:
        bid, _ = asignacion[j]
        volcar_estaticos(-1 if bid == CAPTION else bid, j)
        j_fin = j + 1
        while j_fin < n and asignacion[j_fin][0] == bid:
            j_fin += 1

        if bid == CAPTION:
            tipo, meta = "caption", {}
        else:
            b = bloques_md[bid]
            tipo, meta = b.tipo, dict(b.meta)

        runs = []
        k = j
        while k < j_fin:
            _, formato = asignacion[k]
            if not formato:
                k += 1
                continue
            k_fin = k + 1
            while k_fin < j_fin and asignacion[k_fin][1] == formato:
                k_fin += 1
            runs.append({"inicio": k, "fin": k_fin, "formato": list(formato)})
            k = k_fin

        bloque_out = {"tipo": tipo, "inicio": j, "fin": j_fin, **meta}
        if runs:
            bloque_out["runs"] = runs
        bloques_out.append(bloque_out)
        j = j_fin

    volcar_estaticos(10**9, n)
    return {"bloques": bloques_out}


def main():
    if len(sys.argv) != 4:
        print("Uso: python build_bloques.py <archivo.md> <archivo.json> <salida.json>")
        sys.exit(1)
    md_path, json_path, salida = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    resultado = construir(md_path, json_path)
    salida.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Generado: {salida} ({len(resultado['bloques'])} bloques)")


if __name__ == "__main__":
    main()
