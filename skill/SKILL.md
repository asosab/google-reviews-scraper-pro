---
name: google-reviews-scraper
description: Scrapea negocios y reseñas de Google Maps (Google Reviews) por nicho/ciudad usando la app googleReviews (repo tercero + scripts propios). Use when the user asks to scrape/recolectar/descargar negocios o reseñas de Google Maps, o a ejecutar/diagnosticar el pipeline googleReviews: rasterize, discover, build_config, start.py scrape, enrich_mongo, sync_negocio_reviews, investigaciones. Incluye la calibración probada (tope 120, zoom-max 17, pane de reseñas), los 4 parches locales del repo y el orden de ejecución obligatorio.
---

# googleReviews — operación del pipeline

App: clon del repo de terceros `google-reviews-scraper-pro` (v1.2.3) + scripts propios.
Obtiene **negocios** (descubrimiento por cuadrícula) y **reseñas** (scraping headless)
desde Google Maps y los guarda en **MongoDB** (SQLite como capa intermedia).

> **Regla fija:** nunca debe verse una ventana de Chrome. SeleniumBase **UC mode en Linux
> ignora `headless=True`** (fuerza headed → Xvfb). Por eso `rasterize.py` y `discover.py` se
> **re-ejecutan solos bajo `xvfb-run`** (`-screen 0 1600x1200x24`) al arrancar en Linux, y
> `start.py` del repo debe correrse igual (`xvfb-run -a -s "-screen 0 1600x1200x24" python start.py ...`).
> No lanzar scraping sin display virtual; ningún chrome debe quedar en `DISPLAY=:0`.

## Prerrequisitos (validar antes de tocar nada)

1. **App presente.** El repo `googleReviews/` es autocontenido: app (terceros) + scripts
   propios (`googleReviews/scripts/`) + este skill (`googleReviews/skill/`) + `patches/`.
   Ubicarlo o clonarlo; si falta, pedir al usuario la ruta — no asumir.
2. **venv.** Usar SIEMPRE `googleReviews/.venv/bin/python`, nunca `python` suelto.
   Debe tener `seleniumbase`, `pymongo`, `PyYAML`, `requests` (ver `requirements.txt`).
3. **Chrome + driver.** El repo incluye el driver avc de seleniumbase; no gestionar
   chromedriver a mano. Primera ejecución que falle → `seleniumbase install chromedriver`.
4. **MongoDB local** en `mongodb://admin:admin@127.0.0.1:27017/?authSource=admin`
   (por defecto; sobrescribible con `--mongo-uri`). BD `google_reviews`.
5. **Regla de etiqueta:** Google limita por IP (~120 resultados con "fin de lista"
   falso) y degrada tras corridas seguidas. **Nunca lanzar rastreos completos
   back-to-back al mismo cluster**; respetar pausas; espaciar retries.

## Orden de ejecución OBLIGATORIO

Cada fase produce la entrada de la siguiente. Omitir una fase rompe el pipeline.

```
F1 Descubrimiento (rasterize.py o discover.py)  → discovery.json + CSV
F2 Config (build_config.py)                     → config.yaml
F3 Scrape de reseñas (start.py scrape)           → SQLite + Mongo (reseñas)
F4 Enriquecimiento (enrich_mongo.py)             → Mongo (location GeoJSON, website, email)
F5 Sincronizar reseñas (sync_negocio_reviews.py) → Mongo (negocio_reviews + negocio_id)
F6 Investigación (investigaciones.py)           → negocios_investigacion + investigacion_ID
F7 Verificación (mongosh)                        → conteos + sanidad
```

Fases antes de ejecutar algo pesado, siempre: `--dry-run` (rasterize) y un **smoke**
de 1-2 negocios (scrape) para validar selectores sin gastar la cuota.

## Arranque en un solo comando — `service.sh investigacion`

`googleReviews/scripts/investigar.py` encadena **todo** el pipeline (F1→F6) para una
investigación (nueva o existente) a partir de un área geográfica y keywords. Se lanza
SIEMPRE por `service.sh` (cwd = `googleReviews/`, su `.venv` en el PATH; ver AGENTS.md):

```bash
# nueva — keywords + localidad y radio (círculo)
googleReviews/service.sh investigacion --nombre "Inmobiliarias SCZ" \
    --keywords "inmobiliaria,agencia inmobiliaria" \
    --localidad "Santa Cruz de la Sierra, Bolivia" --radio 15

# círculo por coordenada
googleReviews/service.sh investigacion --nombre "..." --keywords "..." \
    --lat -17.78 --lng -63.18 --radio 10

# polígono de coordenadas (lat,lng;lat,lng;...)
googleReviews/service.sh investigacion --nombre "..." --keywords "..." \
    --poligono "-17.80,-63.20;-17.80,-63.10;-17.70,-63.10"

# existente: reutiliza por nombre (mismo --nombre) o por id
googleReviews/service.sh investigacion --id <objectid> --keywords "..." \
    --localidad "..." --radio 15
```

- **Geometrías excluyentes:** `--localidad [--radio]` | `--lat/--lng --radio` |
  `--poligono "lat,lng;..."` (≥3 puntos). Sin `--radio`, la localidad usa su bbox.
- **Nueva vs existente:** busca en `negocios_investigacion` por `--nombre` (o `--id`).
  Si no existe la crea (`--new --tag none`); al terminar registra `--update`
  (duración + conteos) y taguea los negocios de sus keywords sin robar de otras.
- **Colecciones (clave):** el descubrimiento escribe en la colección **compartida
  `negocios`**; las reseñas en la colección por investigación (**slug** del nombre,
  p.ej. `inmobiliarias_scz`). Nunca la misma para ambas (los docs de negocio no tienen
  `review_id` y rompen F5).
- **Zoom:** default `--zoom 16 --zoom-max 17` (z18/z19 saturan sin fin).
- **Smoke / límites:** `--limit-searches N` (F1) y `--max-reviews N` (F3); `--fases 123456`
  para cortar (`--fases 1` = solo descubrir).
- **`--dry-run`:** imprime la rejilla (F1) sin tocar Google ni Mongo.
- **Termina solo:** al acabar la investigación el proceso sale y `service.sh status`
  reporta *detenido*; no hay que matar nada a mano.

## Fase 1 — Descubrimiento

Modo recomendado (cobertura completa): **rastrillaje por cuadrícula**.

```bash
googleReviews/.venv/bin/python googleReviews/scripts/rasterize.py \
  --bbox "s,w,n,e" \                    # o --area "<ciudad>" (geocodifica vía Nominatim)
  --keywords "dental,odontología" \
  --zoom 16 --zoom-max 17 \
  --overlap 0.15 \
  --out-dir googleReviews/scripts/raster/<caso> \
  --mongo-collection negocios           # upsert de negocios en Mongo

# paso previo obligatorio:
... --dry-run                            # imprime la cuadrícula sin tocar Google
```

**Calibración (probada sobre Santa Cruz de la Sierra, 2026-10):**

| Parámetro | Valor | Por qué |
|---|---|---|
| `--zoom 16` | base | celda ≈ 3.18×2.07 km para viewport 1400×913 y lat -17.8 |
| `--zoom-max 17` | **obligatorio** | z18/z19 saturan sin fin en zonas densas (explosión de subdivisión: 1 celda base → 46 celdas z19). A z17 el tope se rompe (120→61/41). |
| `--cap 120` | predeterminado del script | Google devuelve ~120 máx. con "end of list" **falso** en celdas densas |
| `--overlap 0.15` | recomendado | compensa tarjetas que caen en bordes de celda |
| bbox | **mancha urbana**, no el municipio | Nominatim da el municipio entero (56×55 km = 672 celdas). Acotar a la zona urbanizada o usar `--polygon`. |

- **Reanudable:** checkpoint automático en `<out-dir>/cells.jsonl` por `(keyword, celda)`.
  Reanudar = repetir el mismo comando (salta las celdas hechas).
- Subdivisión automática: si `raw ≥ 0.9 × cap`, parte la celda en 4 a zoom+1.
- Salidas: `discovery.json`, `discovery.csv`, `report.json`, `cells.jsonl/csv`.
- **Si el viewport del navegador difiere** (los px determinan el tamaño de celda y el
  ancho del feed), recalcular: `px_por_metro = 156543.03392 * cos(lat) / 2^zoom`;
  celda = `viewport_px × px_por_metro`. No usar la tabla de memoria del skill.

Modo simple (sondeo, NO cobertura completa): `discover.py --keyword X --location YZ`.

## Fase 2 — Config del scraper

```bash
googleReviews/.venv/bin/python googleReviews/scripts/build_config.py \
  --discovery googleReviews/scripts/raster/<caso>/discovery.json \
  --out googleReviews/scripts/raster/<caso>/config.yaml \
  --keyword "dental" --mongo-collection dental
```

Prefere: añade site/location/custom_params a los negocios. `config.yaml` incluye settings
clave: `sort_by: relevance` (evita el menú de orden), `scroll_idle_limit`, y la sección
`mongodb` (database/collection/sync_mode).

## Fase 3 — Scrape de reseñas

```bash
cd googleReviews && .venv/bin/python start.py \
  --config «$PWD/../googleReviews/scripts/raster/<caso>/config.yaml»
```

Flujo interno (por negocio): warm-up de sesión → navegación search-based (bypass de la
"vista limitada") → click en la pestaña de reseñas (por texto, multilenguaje) → pane de
reseñas (el de MÁS `div[data-review-id]`) → scroll CDP `mouseWheel` (lazy-load) →
parseo `div[data-review-id]` → SQLite + sync a Mongo.

El repo de terceros **arrastra 4 bugs** que solo funcionan gracias a los parches locales
(ver `patches/`, aplicables con `git apply` si se clona fresco):

1. `data_storage.py` — pymongo 4.12 lee toda opción `tls*` como TLS → `SSL handshake
   failed`. Fix: pasar la opción solo si es `True`.
2. `click_reviews_tab` — etiqueta variable (Reseñas/Opiniones/…) → 3 reseñas máx.
   Fix: click por texto/keywords multilenguaje.
3. `_wheel_scroll` — el scroll JS no dispara el lazy-load. Fix: evento nativo CDP
   `Input.dispatchMouseEvent mouseWheel`.
4. Selección de pane — caía en el pane de resumen (~5 reseñas). Fix: refinar al pane
   con más `div[data-review-id]`.

## Fase 4 — Enriquecimiento

```bash
googleReviews/.venv/bin/python googleReviews/scripts/enrich_mongo.py \
  --discovery googleReviews/scripts/raster/<caso>/discovery.json \
  --db google_reviews --collection dental --crawl-emails
```

- Une por **prefijo hex** del place_id (`0x:0` del scraper ↔ `0x:0x<cid>` del discovery).
- `location` GeoJSON **en orden `[lng, lat]`** (no [lat, lng]) + índice `location_2dsphere`.
- Emails: Google Maps no los publica; se crawlean del sitio web (mailto + regex +
  unescape HTML; se descartan emails de plataformas facebook/linktr.ee/wa.me).

## Fase 5 — Sincronizar reseñas → `negocio_reviews`

```bash
googleReviews/.venv/bin/python googleReviews/scripts/sync_negocio_reviews.py \
  --collections dental,smoke \
  --negocios-collection negocios --reviews-collection negocio_reviews
```

- Copia cada reseña (con **todos sus campos**) a `negocio_reviews` añadiendo
  `negocio_id` (el `_id` del negocio en `negocios`), uniendo por **prefijo hex** del
  `place_id`.
- Si el negocio no existe aún en `negocios` (p. ej. llegó por otra búsqueda), lo **crea
  desde los datos de la reseña** (name/company, category, address, phone, lat/lng→GeoJSON).
- Idempotente: dedup por `review_id` (índice único). Re-ejecutar tras cada scrape.
- `rasterize.py` re-usa ahora el mismo prefijo hex para upsert → no crea duplicados de
  esos negocios en futuros rastreos.

## Fase 6 — Investigación (`negocios_investigacion`)

```bash
googleReviews/.venv/bin/python googleReviews/scripts/investigaciones.py \
  --new --nombre "NICHO CIUDAD" --description "..." \
  --keywords "kw1,kw2" --bbox "s,w,n,e" --duracion-seg N --tag all
```

- Crea una entrada por investigación: `activa` (bool), `fecha_pedida`, `duracion_primera_vez_seg`,
  `registros_negocios`, `registros_negocio_reviews`, `actualizaciones[]`.
- Marca los negocios con `negocios.investigacion_ID`. `--update <id>` taguea los negocios de
  las keywords de la investigación (sin robar de otras) y agrega
  `{fecha, registros_revisados, duracion_seg}`. `--list` para ver; `--set-activa ID true|false`
  para activar/desactivar.
- `--tag all` para cubrir todo el corpus actual; `--tag keywords` solo los de las keywords.
- Investigación "activa" = se espera que su rastreo siga corriendo/actualizándose sola.

## Fase 7 — Verificación

```bash
mongosh "mongodb://admin:admin@127.0.0.1:27017/?authSource=admin" --quiet --eval '
  const d=db.getSiblingDB("google_reviews");
  d.negocios.countDocuments();
  d.dental.countDocuments();
  // review_count es STRING: ordenar con $toInt
  d.negocios.aggregate([{$addFields:{rc:{$toInt:{$ifNull:["$review_count","0"]}}}},
    {$match:{rc:{$gt:0}}},{$sort:{rc:-1}},{$limit:10}]).toArray().forEach(p=>print(p.rc, p.name));
'
```

## Trampas de sanidad (verificar siempre)

- **`review_count` es string** → ordenar con `$toInt`, no como texto (`"9" > "74"`).
- Resultados de búsqueda mezclan rubros (Google devuelve médicos/radiológicos para
  "dental") → filtrar por campo `category` si el cliente exige rubro exacto.
- Un negocio con 0 reseñas suele ser legítimo (ordenamos por review_count desc).
- No commitear en el repo de terceros; los cambios son parches locales.
- Cobertura: comparar el conteo de una celda solo vs. la búsqueda de toda la ciudad
  para medir cuánto se gana (referencia SCZ: 98 ciudad → 233 en 1 celda del centro).