#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
align_md_txt.py
================

Herramienta de análisis (no genera nada por sí sola): compara un .md con
formato contra el .json de sincronización ya generado a partir del .txt
narrado, para localizar con precisión dónde el texto hablado coincide
palabra por palabra con el markdown (se puede mapear el formato
automáticamente) y dónde diverge (tablas verbalizadas a mano, bloques de
código omitidos, párrafos introductorios agregados solo para TTS, etc.),
que requieren una decisión manual de cómo mostrarlos.

Uso:
    python herramientas/align_md_txt.py <archivo.md> <archivo.json>
"""

import json
import re
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

WORD_RE = re.compile(
    r"[A-Za-zÀ-ÖØ-öø-ÿ0-9_]+(?:-[A-Za-zÀ-ÖØ-öø-ÿ0-9_]+)*"  # palabras y compuestos-con-guion
    r"|[/=]"  # edge-tts lee "/" y "=" como su propia palabra, lleven o no espacios alrededor
)


def normalizar_palabra(w: str) -> str:
    return w.lower()


@dataclass
class Token:
    texto: str          # texto original tal cual aparece en el md (sin normalizar)
    bloque_id: int
    formatos: tuple      # ej. ('bold',), ('code',), ()


@dataclass
class Bloque:
    id: int
    tipo: str            # h1, h2, h3, p, li, li2, table-row, code, hr, aside
    meta: dict = field(default_factory=dict)


INLINE_RE = re.compile(
    r"\*\*(?P<b>.+?)\*\*"
    r"|~~(?P<s>.+?)~~"
    r"|`(?P<c>[^`]+)`"
    r"|(?<![\*\w])\*(?=\S)(?P<i>.+?)(?<=\S)\*(?![\*\w])"
    r"|\[(?P<lt>[^\]]+)\]\((?P<lu>[^)]*)\)"
)


def segmentos(texto: str, formatos: tuple = ()) -> list:
    """
    Divide una línea de markdown en [(texto, formatos)], resolviendo negrita,
    cursiva, ~~tachado~~, `código` y [enlaces](url) (de un enlace solo queda el texto).
    Es la única definición de "qué es texto visible": la usan tanto el
    analizador como la narración, para que siempre coincidan.
    """
    salida = []
    pos = 0
    for m in INLINE_RE.finditer(texto):
        if m.start() > pos:
            salida.append((texto[pos : m.start()], formatos))
        if m.group("b") is not None:
            salida += segmentos(m.group("b"), formatos + ("bold",))
        elif m.group("s") is not None:
            salida += segmentos(m.group("s"), formatos + ("strike",))
        elif m.group("c") is not None:
            salida.append((m.group("c"), formatos + ("code",)))
        elif m.group("i") is not None:
            salida += segmentos(m.group("i"), formatos + ("italic",))
        else:
            salida += segmentos(m.group("lt"), formatos + ("link",))
        pos = m.end()
    if pos < len(texto):
        salida.append((texto[pos:], formatos))
    return [(t, tuple(sorted(set(f)))) for t, f in salida]


def texto_plano(texto: str) -> str:
    return "".join(t for t, _ in segmentos(texto))


RE_HR = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
RE_CALLOUT = re.compile(r"^\[![\w-]+\][+-]?\s*")
RE_TAREA = re.compile(r"^\[([ xX])\]\s+")


def tokenizar_md(md_text: str):
    """Devuelve (tokens, bloques) recorriendo el markdown línea por línea."""
    lines = md_text.split("\n")
    tokens: list[Token] = []
    bloques: list[Bloque] = []
    bloque_id = -1

    def nuevo_bloque(tipo, **meta):
        nonlocal bloque_id
        bloque_id += 1
        bloques.append(Bloque(bloque_id, tipo, meta))
        return bloque_id

    def emitir_label(texto, bid):
        for m in WORD_RE.finditer(texto):
            tokens.append(Token(m.group(0), bid, ("label",)))

    def emitir_texto_formateado(texto, bid):
        for seg, formatos in segmentos(texto):
            for m in WORD_RE.finditer(seg):
                tokens.append(Token(m.group(0), bid, formatos))

    i = 0
    if lines and lines[0].strip() == "---":  # bloque de metadatos (front matter)
        for k in range(1, min(len(lines), 40)):
            if lines[k].strip() == "---":
                i = k + 1
                break

    pila_sangria: list[int] = []  # niveles de sangría de la lista actual
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()

        if line.startswith("```"):
            lang = line[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            nuevo_bloque("code", lang=lang, contenido="\n".join(code_lines))
            pila_sangria = []
            i += 1
            continue

        if not line or line.startswith("<!--"):
            i += 1
            continue

        if RE_HR.match(line):
            nuevo_bloque("hr")
            pila_sangria = []
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            bid = nuevo_bloque(f"h{len(m.group(1))}")
            emitir_texto_formateado(m.group(2), bid)
            pila_sangria = []
            i += 1
            continue

        if line.startswith(">"):
            texto = RE_CALLOUT.sub("", re.sub(r"^(>\s?)+", "", line))
            if texto.strip():
                bid = nuevo_bloque("quote")
                emitir_texto_formateado(texto, bid)
            pila_sangria = []
            i += 1
            continue

        if line.startswith("|"):
            filas = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                filas.append(lines[i].strip())
                i += 1
            filas = [f for f in filas if not re.match(r"^\|[\s:|-]+\|$", f)]
            if filas:
                encabezado = [texto_plano(c.strip()) for c in filas[0].strip("|").split("|")]
                for fila in filas[1:]:
                    celdas = [c.strip() for c in fila.strip("|").split("|")]
                    bid = nuevo_bloque("table-row", encabezado=encabezado)
                    for col_idx, celda in enumerate(celdas):
                        # El texto narrado dice cada celda como "Encabezado: valor."
                        if col_idx < len(encabezado):
                            emitir_label(encabezado[col_idx], bid)
                        emitir_texto_formateado(celda, bid)
            pila_sangria = []
            continue

        m = re.match(r"^(\s*)[-*+]\s+(.*)$", raw)
        n = None if m else re.match(r"^(\s*)(\d+)[.)]\s+(.*)$", raw)
        if m or n:
            sangria = len(m.group(1) if m else n.group(1).replace("\t", "    "))
            while pila_sangria and sangria < pila_sangria[-1]:
                pila_sangria.pop()
            if not pila_sangria or sangria > pila_sangria[-1]:
                pila_sangria.append(sangria)
            nivel = min(len(pila_sangria), 4)
            texto = m.group(2) if m else n.group(3)
            meta = {"nivel": nivel}
            if n:
                meta["numero"] = n.group(2)
            t = RE_TAREA.match(texto) if m else None
            if t:
                meta["tarea"] = "hecha" if t.group(1) in "xX" else "pendiente"
                texto = texto[t.end():]
            bid = nuevo_bloque("li-num" if n else "li", **meta)
            emitir_texto_formateado(texto, bid)
            i += 1
            continue

        pila_sangria = []
        bid = nuevo_bloque("p")
        emitir_texto_formateado(line, bid)
        i += 1

    return tokens, bloques


def cargar_palabras_json(json_path: Path):
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return [w["texto"] for w in data["palabras"]]


def main():
    if len(sys.argv) != 3:
        print("Uso: python align_md_txt.py <archivo.md> <archivo.json>")
        sys.exit(1)

    md_path = Path(sys.argv[1])
    json_path = Path(sys.argv[2])

    md_text = md_path.read_text(encoding="utf-8")
    tokens, bloques = tokenizar_md(md_text)
    md_words = [normalizar_palabra(t.texto) for t in tokens]

    json_words_raw = cargar_palabras_json(json_path)
    json_words = [normalizar_palabra(w) for w in json_words_raw]

    sm = SequenceMatcher(a=md_words, b=json_words, autojunk=False)
    opcodes = sm.get_opcodes()

    def es_trivial(md_frag: str, json_frag: str) -> bool:
        a = re.sub(r"[^a-zà-öø-ÿ0-9]", "", md_frag.lower())
        b = re.sub(r"[^a-zà-öø-ÿ0-9]", "", json_frag.lower())
        return a == b

    total_md = len(md_words)
    total_json = len(json_words)
    iguales = 0
    reales = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            iguales += j2 - j1
            continue
        md_fragment = " ".join(t.texto for t in tokens[i1:i2])
        json_fragment = " ".join(json_words_raw[j1:j2])
        if es_trivial(md_fragment, json_fragment):
            iguales += j2 - j1
            continue
        reales.append((tag, i1, i2, j1, j2, md_fragment, json_fragment))

    print(f"Archivo: {md_path.name}")
    print(f"Palabras en .md (tokenizadas): {total_md}")
    print(f"Palabras en .json (habladas):  {total_json}")
    print(f"Coincidencia (incl. triviales): {iguales} ({100*iguales/max(total_json,1):.1f}% del audio)")
    print(f"Divergencias reales a revisar:  {len(reales)}\n")

    print("=== Regiones NO coincidentes (requieren decisión manual) ===\n")
    for tag, i1, i2, j1, j2, md_fragment, json_fragment in reales:
        bloque_ids = sorted({t.bloque_id for t in tokens[i1:i2]}) if i1 < i2 else []
        tipos = sorted({bloques[b].tipo for b in bloque_ids}) if bloque_ids else ["(sin bloque md)"]
        print(f"--- tag={tag} | md[{i1}:{i2}] tipos={tipos} | json[{j1}:{j2}] ---")
        print(f"  MD  : {md_fragment[:200]}")
        print(f"  JSON: {json_fragment[:250]}")
        print()


if __name__ == "__main__":
    main()
