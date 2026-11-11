#!/usr/bin/env python3
"""Rastreador de cambios normativos ICA/ReteICA por municipio.

Lee urls.csv (municipio, tipo, url), descarga cada página, compara contra la
versión guardada en state/ y genera informe_cambios.md (+ correo opcional).
Uso:  python rastreador_ica.py            # revisa todo
      python rastreador_ica.py --sin-correo
"""
import csv, hashlib, json, os, re, smtplib, ssl, sys, time, difflib, datetime
from email.mime.text import MIMEText
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
UA = {"User-Agent": "Mozilla/5.0 (compatible; RastreadorICA/1.0)"}
# Palabras que indican que el cambio es tributario relevante
CLAVES = ["industria y comercio", "ica", "reteica", "retención", "autorretención",
          "tarifa", "acuerdo", "estatuto tributario", "calendario tributario",
          "resolución", "decreto", "declaración", "plazo", "vencimiento",
          "sanción", "formulario", "umbral", "uvt", "avisos y tableros"]
DESCUBRIR = ["hacienda", "impuesto", "industria y comercio", "industria-y-comercio", "normatividad", "normativa",
             "acuerdos", "estatuto", "calendario", "tributari", "reteica", "concejo", "rentas", "tesorer"]
MAX_SUB = 12   # máximo de subpáginas vigiladas por portal
DOC_RE = re.compile(r"\.(pdf|docx?)($|\?)", re.I)
VENTANA_DIAS = int(os.getenv("VENTANA_DIAS", "30"))   # solo se reporta lo de los últimos N días
MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
         "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
_M = "|".join(MESES)
RE_F1 = re.compile(rf"\b(\d{{1,2}})\s*(?:de\s+)?({_M})\s*(?:de|del|,)?\s*((?:19|20)\d{{2}})\b", re.I)   # 27 de diciembre de 2018
RE_F2 = re.compile(rf"\b({_M})\s*(\d{{1,2}})\s*(?:de|del|,)?\s*((?:19|20)\d{{2}})\b", re.I)            # diciembre 27 de 2018
RE_F3 = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.]((?:19|20)\d{2})\b")                              # 27/12/2018
RE_F4 = re.compile(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b")                                             # 2018-12-27
RE_ANIO = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")

def _d(y, m, d):
    try: return datetime.date(int(y), int(m), int(d))
    except ValueError: return None

def fechas_en(texto):
    """Devuelve (fechas_completas, años_sueltos) encontrados en un texto."""
    f = []
    for m in RE_F1.finditer(texto): f.append(_d(m[3], MESES[m[2].lower()], m[1]))
    for m in RE_F2.finditer(texto): f.append(_d(m[3], MESES[m[1].lower()], m[2]))
    for m in RE_F3.finditer(texto): f.append(_d(m[3], m[2], m[1]))
    for m in RE_F4.finditer(texto): f.append(_d(m[1], m[2], m[3]))
    f = [x for x in f if x]
    anios = [int(a) for a in RE_ANIO.findall(texto)]
    return f, anios

def es_reciente(texto, hoy=None):
    """True/False si el texto trae fecha; None si no trae ninguna (no se puede saber)."""
    hoy = hoy or datetime.date.today()
    corte = hoy - datetime.timedelta(days=VENTANA_DIAS)
    f, anios = fechas_en(texto)
    if f:
        return max(f) >= corte                    # fecha completa: dentro de la ventana (o futura)
    anios = [a for a in anios if a <= hoy.year + 2]
    if anios:
        return max(anios) >= corte.year           # solo año: descarta años anteriores a la ventana
    return None

def clave_doc(titulo, url):
    """Identifica un documento por su título (las URL de muchos portales cambian en cada visita)."""
    t = re.sub(r"[^a-z0-9áéíóúñ]+", " ", (titulo or "").lower()).strip()
    return t or url

def docs_previos(previo):
    """Conjunto de claves de documentos ya vistos (compatible con el formato anterior url->título)."""
    ks = set(previo.get("docs2", {}))
    for u, t in previo.get("docs", {}).items():
        ks.add(clave_doc(t, u))
    return ks

def limpiar(html):
    s = BeautifulSoup(html, "html.parser")
    for t in s(["script", "style", "noscript", "header", "footer", "nav"]):
        t.decompose()
    texto = s.get_text("\n")
    lineas = [re.sub(r"\s+", " ", l).strip() for l in texto.splitlines()]
    lineas = [l for l in lineas if len(l) > 3]
    return lineas, s

def enlaces_doc(soup, base):
    out = {}
    for a in soup.find_all("a", href=True):
        h = urljoin(base, a["href"])
        t = re.sub(r"\s+", " ", a.get_text()).strip()
        if DOC_RE.search(h) or any(k in t.lower() for k in ("acuerdo", "resolución", "calendario", "estatuto")):
            out[h] = t[:160]
    return out

def descargar(url):
    for i in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=30)
            if r.status_code == 200:
                return r.content, None
            err = f"HTTP {r.status_code}"
        except Exception as e:
            err = type(e).__name__
        time.sleep(2 * (i + 1))
    return None, err

def relevancia(lineas_nuevas):
    txt = " ".join(lineas_nuevas).lower()
    return [k for k in CLAVES if k in txt]

def clave_estado(mun, tipo, url):
    h = hashlib.sha1(f"{mun}|{tipo}|{url}".encode()).hexdigest()[:16]
    return os.path.join(STATE, h + ".json")

def descubrir(fila):
    """Para filas tipo 'Portal': encuentra enlaces internos con palabras tributarias y los convierte en filas a vigilar."""
    html, err = descargar(fila["url"])
    if html is None:
        return [dict(fila, tipo="Portal")]          # se reportará como ERROR
    _, soup = limpiar(html)
    dom = re.sub(r"^https?://(www\.)?", "", fila["url"]).split("/")[0]
    vistos, subs = set(), []
    for a in soup.find_all("a", href=True):
        h = urljoin(fila["url"], a["href"]).split("#")[0]
        t = re.sub(r"\s+", " ", a.get_text()).strip()
        if dom not in h or DOC_RE.search(h) or h in vistos or h.rstrip("/") == fila["url"].rstrip("/"):
            continue
        if any(k in (h + " " + t).lower() for k in DESCUBRIR):
            vistos.add(h); subs.append({"municipio": fila["municipio"], "tipo": "Sub: " + (t[:40] or "enlace"), "url": h})
    return subs[:MAX_SUB] or [dict(fila, tipo="Portal (sin subpáginas tributarias halladas)")]

def revisar(fila):
    mun, tipo, url = fila["municipio"], fila["tipo"], fila["url"]
    html, err = descargar(url)
    ruta = clave_estado(mun, tipo, url)
    previo = json.load(open(ruta, encoding="utf-8")) if os.path.exists(ruta) else None
    if html is None:
        falls = (previo or {}).get("fallos", 0) + 1
        if previo:
            previo["fallos"] = falls; json.dump(previo, open(ruta, "w", encoding="utf-8"), ensure_ascii=False)
        return {"municipio": mun, "tipo": tipo, "url": url, "estado": "ERROR", "detalle": f"{err} (intento consecutivo {falls})"}
    lineas, soup = limpiar(html)
    docs = enlaces_doc(soup, url)
    hoy = datetime.date.today().isoformat()
    vistos_prev = (previo or {}).get("docs2", {})
    docs2 = {}
    for u, t in docs.items():
        k = clave_doc(t, u)
        docs2[k] = vistos_prev.get(k) or {"t": t, "u": u, "v": hoy}     # "v" = primera vez que se vio
    nuevo = {"lineas": lineas, "docs": docs, "docs2": docs2, "fecha": hoy, "fallos": 0}
    json.dump(nuevo, open(ruta, "w", encoding="utf-8"), ensure_ascii=False)
    if previo is None:
        return {"municipio": mun, "tipo": tipo, "url": url, "estado": "BASE", "detalle": "Primera lectura guardada"}
    # --- documentos: solo los que no se habían visto Y no son claramente antiguos
    ya = docs_previos(previo)
    docs_nuevos, descartados = {}, 0
    for k, d in docs2.items():
        if k in ya: continue
        rec = es_reciente(d["t"] + " " + d["u"])
        if rec is False: descartados += 1; continue      # trae fecha/año viejo -> se ignora
        docs_nuevos[d["u"]] = d["t"] + ("" if rec else "  (sin fecha visible)")
    # --- texto: solo líneas agregadas con palabras tributarias y que no sean de fechas viejas
    set_prev = set(previo["lineas"])
    agreg = [l for l in lineas if l not in set_prev and len(l) >= 15 and es_reciente(l) is not False]
    quit_ = [l for l in previo["lineas"] if l not in set(lineas)][:8]
    claves = relevancia(agreg + list(docs_nuevos.values()))
    titulos = {d.split("  (sin fecha")[0].strip() for d in docs_nuevos.values()}
    agreg_rel = [l for l in agreg if relevancia([l]) and l.strip() not in titulos]
    if not docs_nuevos and not (claves and agreg_rel):
        return {"municipio": mun, "tipo": tipo, "url": url, "estado": "SIN CAMBIOS", "detalle": f"ignorados antiguos: {descartados}"}
    return {"municipio": mun, "tipo": tipo, "url": url, "estado": "CAMBIO RELEVANTE", "claves": claves,
            "agregado": agreg_rel[:10], "quitado": quit_, "docs_nuevos": docs_nuevos}

def informe(res):
    hoy = datetime.date.today()
    desde = (hoy - datetime.timedelta(days=VENTANA_DIAS)).strftime("%d/%m/%Y")
    rel = [r for r in res if r["estado"] == "CAMBIO RELEVANTE"]
    err = [r for r in res if r["estado"] == "ERROR"]
    L = [f"# Rastreador ICA – informe {hoy.isoformat()}", "",
         f"Solo normativa nueva o actualizada desde {desde} (últimos {VENTANA_DIAS} días). "
         f"Páginas revisadas: {len(res)} | Con novedades: {len(rel)} | No se pudieron leer: {len(err)}", ""]
    if rel:
        L.append("## ⚠️ NOVEDADES NORMATIVAS (revisar)")
        for r in rel:
            L += ["", f"### {r['municipio']} – {r['tipo']}", r["url"]]
            for u, t in r["docs_nuevos"].items(): L.append(f"- NUEVO: {t or 'sin título'} → {u}")
            for a in r["agregado"]: L.append(f"- Texto nuevo/actualizado: {a[:200]}")
    else:
        L.append("✅ Sin novedades normativas en los últimos %d días." % VENTANA_DIAS)
    if err:
        L += ["", f"_({len(err)} páginas no se pudieron leer; el detalle está en informe_cambios.md del repositorio)_"]
    L += ["", "_Alerta automática. Valide cada novedad contra la norma oficial antes de actuar._"]
    return "\n".join(L)

def detalle_errores(res):
    err = [r for r in res if r["estado"] == "ERROR"]
    return "\n".join(["", "## Páginas que no se pudieron leer"] + [f"- {r['municipio']} – {r['tipo']}: {r['detalle']} – {r['url']}" for r in err]) if err else ""

def enviar(asunto, cuerpo):
    h, u, p, to = (os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    if not all((h, u, p, to)):
        print("Correo no configurado (SMTP_HOST/SMTP_USER/SMTP_PASS/MAIL_TO); solo se guardó el informe."); return
    m = MIMEText(cuerpo, "plain", "utf-8"); m["Subject"] = asunto; m["From"] = u; m["To"] = to
    with smtplib.SMTP_SSL(h, int(os.getenv("SMTP_PORT", "465")), context=ssl.create_default_context()) as s:
        s.login(u, p); s.sendmail(u, to.split(","), m.as_string())
    print("Correo enviado a", to)

def main():
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(HERE, "urls.csv"), encoding="utf-8-sig") as f:
        todas = [r for r in csv.DictReader(f) if r.get("url", "").startswith("http")]
    # activo = NO (o 0/FALSE) -> el municipio se omite sin borrar su historial
    filas = [r for r in todas if (r.get("activo") or "SI").strip().upper() not in ("NO", "N", "0", "FALSE", "INACTIVO")]
    print(f"Municipios en lista: {len(todas)} | activos: {len(filas)} | omitidos: {len(todas) - len(filas)}")
    res = []
    expandidas = []
    for fila in filas:
        expandidas += descubrir(fila) if fila["tipo"].strip().lower() == "portal" else [fila]
    filas = expandidas
    for fila in filas:
        print("Revisando", fila["municipio"], fila["tipo"])
        res.append(revisar(fila)); time.sleep(1)
    texto = informe(res)
    open(os.path.join(HERE, "informe_cambios.md"), "w", encoding="utf-8").write(texto + detalle_errores(res))
    print(texto + detalle_errores(res))
    rel = sum(r["estado"] == "CAMBIO RELEVANTE" for r in res)
    if "--sin-correo" not in sys.argv and rel:
        enviar(f"Rastreador ICA: {rel} municipio(s) con cambio relevante", texto)

if __name__ == "__main__":
    main()
