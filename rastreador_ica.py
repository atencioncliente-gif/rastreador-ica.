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
    nuevo = {"lineas": lineas, "docs": docs, "fecha": datetime.date.today().isoformat(), "fallos": 0}
    json.dump(nuevo, open(ruta, "w", encoding="utf-8"), ensure_ascii=False)
    if previo is None:
        return {"municipio": mun, "tipo": tipo, "url": url, "estado": "BASE", "detalle": "Primera lectura guardada"}
    agreg = [l[2:] for l in difflib.ndiff(previo["lineas"], lineas) if l.startswith("+ ")]
    quit_ = [l[2:] for l in difflib.ndiff(previo["lineas"], lineas) if l.startswith("- ")]
    docs_nuevos = {u: t for u, t in docs.items() if u not in previo.get("docs", {})}
    if not agreg and not quit_ and not docs_nuevos:
        return {"municipio": mun, "tipo": tipo, "url": url, "estado": "SIN CAMBIOS", "detalle": ""}
    claves = relevancia(agreg + list(docs_nuevos.values()))
    estado = "CAMBIO RELEVANTE" if (claves or docs_nuevos) else "CAMBIO MENOR"
    return {"municipio": mun, "tipo": tipo, "url": url, "estado": estado, "claves": claves,
            "agregado": agreg[:15], "quitado": quit_[:8], "docs_nuevos": docs_nuevos}

def informe(res):
    hoy = datetime.date.today().isoformat()
    rel = [r for r in res if r["estado"] == "CAMBIO RELEVANTE"]
    men = [r for r in res if r["estado"] == "CAMBIO MENOR"]
    err = [r for r in res if r["estado"] == "ERROR"]
    base = [r for r in res if r["estado"] == "BASE"]
    L = [f"# Rastreador ICA – informe {hoy}", "",
         f"Páginas revisadas: {len(res)} | Cambios relevantes: {len(rel)} | Cambios menores: {len(men)} | Errores: {len(err)} | Primera lectura: {len(base)}", ""]
    if rel:
        L.append("## ⚠️ MUNICIPIOS CON CAMBIO RELEVANTE (revisar primero)")
        for r in rel:
            L += ["", f"### {r['municipio']} – {r['tipo']}", r["url"], f"Palabras detectadas: {', '.join(r['claves'])}"]
            for u, t in r["docs_nuevos"].items(): L.append(f"- NUEVO DOCUMENTO: {t or 'sin título'} → {u}")
            for a in r["agregado"]: L.append(f"- + {a[:200]}")
    if men:
        L += ["", "## Cambios menores (probablemente sin impacto)"] + [f"- {r['municipio']} – {r['tipo']}: {r['url']}" for r in men]
    if err:
        L += ["", "## Páginas que no se pudieron leer (revisar manualmente o corregir URL)"] + [f"- {r['municipio']} – {r['tipo']}: {r['detalle']} – {r['url']}" for r in err]
    if not (rel or men or err):
        L.append("Sin cambios en ninguna página.")
    L += ["", "_Este informe es una alerta automática. Valide cada cambio contra la norma oficial antes de actuar._"]
    return "\n".join(L)

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
    open(os.path.join(HERE, "informe_cambios.md"), "w", encoding="utf-8").write(texto)
    print(texto)
    rel = sum(r["estado"] == "CAMBIO RELEVANTE" for r in res)
    if "--sin-correo" not in sys.argv and (rel or any(r["estado"] == "ERROR" for r in res)):
        enviar(f"Rastreador ICA: {rel} municipio(s) con cambio relevante", texto)

if __name__ == "__main__":
    main()
