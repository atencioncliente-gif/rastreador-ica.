# Rastreador ICA – instrucciones

## Qué hace
Cada lunes (7:00 a.m. Colombia) revisa las páginas de Hacienda y del Concejo de cada municipio listadas en `urls.csv`,
las compara con la semana anterior y le envía un correo indicando **en qué municipio cambió algo**, qué texto se agregó
y qué documentos nuevos (acuerdos, resoluciones, calendarios en PDF) aparecieron. Los cambios con palabras clave
(ICA, ReteICA, tarifa, acuerdo, calendario, retención, etc.) se marcan como RELEVANTES.

## Agregar o quitar municipios (sin tocar el código)
- **Agregar:** escriba una fila nueva al final de `urls.csv` (hay 20 filas libres): municipio, `Portal`, URL del portal de la alcaldía, `SI`.
- **Dejar de vigilar** un municipio donde ya no vende: cambie `activo` de `SI` a `NO`. No se borra su historial y puede reactivarlo cuando quiera.
- Las URL de la lista inicial están en 'por verificar': se armaron con la convención oficial `.gov.co`. En la primera corrida, los portales que no abran saldrán en la sección de errores del informe: corrija esa URL y listo.

## Puesta en marcha (una sola vez, ~20 minutos)
1. Revise `urls.csv` (ya trae los 80 municipios con el portal probable). Con tipo `Portal` el bot descubre solo las páginas de Hacienda, impuestos, normatividad, acuerdos, calendario y Concejo del portal (hasta 12 por municipio). Si conoce una página específica de normatividad, agréguela como fila adicional con tipo `Normatividad`.
2. Cree un repositorio privado en GitHub y suba esta carpeta completa.
3. En el repositorio: Settings → Secrets and variables → Actions → New repository secret. Cree:
   SMTP_HOST (ej. smtp.gmail.com), SMTP_USER (correo remitente), SMTP_PASS (contraseña de aplicación),
   MAIL_TO (correo(s) que reciben la alerta, separados por coma).
4. Pestaña Actions → "Rastreador ICA" → Run workflow. La primera corrida solo guarda la línea base (no alerta);
   desde la segunda se detectan cambios.

## Alternativa en su PC
`pip install -r requirements.txt` y luego `python rastreador_ica.py` (use `--sin-correo` para solo generar `informe_cambios.md`).

## Límites (importante)
- Páginas que cargan todo con JavaScript, requieren captcha o bloquean robots pueden dar error: aparecen en el informe
  como "no se pudieron leer" para revisión manual.
- Una página puede cambiar sin que la norma cambie (noticias, banners): por eso hay "relevante" y "menor".
- Si un municipio publica la norma solo en el Concejo o en la Gaceta, agregue esa URL.
- Es una alerta, no una asesoría: valide cada cambio contra el acuerdo oficial con su contador.
