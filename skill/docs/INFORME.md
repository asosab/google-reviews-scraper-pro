# Informe — googleReviews: funcionamiento y métodos de uso

Pipeline completo de scraping de **Google Maps** (negocios + reseñas) usando el repo
de terceros `google-reviews-scraper-pro` (clonado en `googleReviews/`, v1.2.3) + scripts
propios en `googleReviews/scripts/`.

Entorno: WSL2, venv aislado `googleReviews/.venv` (Python 3.14), Chrome headless 155,
SQLite local (`reviews.db`), **MongoDB local** `mongodb://admin:admin@127.0.0.1:27017/?authSource=admin`.

---

## 1. Arquitectura general

```mermaid
flowchart LR
    subgraph Descubrimiento["Descubrimiento de negocios"]
        D["discover.py<br/><i>búsqueda por texto (zona simple)</i>"] --> DO[("discovery.json<br/>+ CSV + businesses.yaml")]
        R["rasterize.py<br/><i>rastrillaje por cuadrícula</i>"] -->|upsert incremental| M1[(("Mongo DB<br/>google_reviews.negocios"))]
        R --> RO[("cells.jsonl · discovery.json<br/>report.json")]
    end

    DO --> B["build_config.py"]
    B --> C["config.yaml<br/>(businesses + settings + Mongo)"]

    C --> S["start.py + modules/scraper.py<br/><i>scrape de reseñas</i>"]
    S --> REV[("SQLite review_db<br/>+ google_reviews.json")]
    S -->|sync por negocio| M2[(("Mongo DB<br/>google_reviews.dental (reseñas)"))]

    E["enrich_mongo.py<br/><i>enriquecer reseñas</i>"] --> M2
    DO -.->|place_id/lat/lng/website| E
```

- **Dos niveles de datos:** `negocios` (los lugares descubiertos, 929) y `reseñas` (los
  reviews scrapeados por negocio, 739 en `dental`).
- El scraper escribe primero a **SQLite** y luego sincroniza a **Mongo** por negocio.

---

## 2. Descubrimiento de negocios

### 2.1 Modo simple — `discover.py`

Una sola búsqueda por texto (`dental Santa Cruz de la Sierra`), scroll del panel lateral
`div[role="feed"]` y parseo de tarjetas `div.Nv2PK`. Adecuado para sondeos rápidos;
**NO da cobertura completa** (búsqueda de ciudad ≈ muestra limitada).

### 2.2 Modo cobertura completa — `rasterize.py`

Es el método recomendado. Divide la zona en celdas del tamaño del viewport del navegador
al zoom elegido, busca cada una y **subdivide las celdas saturadas** (tope ~120) hasta que
dejan de saturarse o se llega al zoom máximo. Es resumible (checkpoint por celda).

```mermaid
flowchart TD
    IN["Entrada<br/>--bbox s,w,n,e (o --area geocodifica) + --keywords"] --> GEO["bbox"]
    GEO --> GRID["Generar cuadrícula<br/>celda = viewport px × m/px(zoom, lat)<br/>paso = celda × (1 − overlap 15%)"]
    GRID --> Q{¿celdas en cola?}
    Q -- sí --> NAV["Navegar<br/>maps/search/&lt;kw&gt;/@lat,lng,zoomz"]
    NAV --> SC["Scroll feed hasta fin / estable<br/>(div[role=feed], +4000px c/2.4s)"]
    SC --> PARSE["Parsear tarjetas div.Nv2PK<br/>place_id · lat/lng · website · teléfono"]
    PARSE --> SAT{"raw ≥ 0.9 × 120?<br/>(saturación)"}
    SAT -- sí y zoom &lt; zoom-max --> SUB["Subdividir en 4 subceldas<br/>zoom+1 → encolar al frente"]
    SUB --> Q
    SAT -- no --> FILTER["Filtro geoespacial<br/>descartar fuera de bbox/polígono"]
    FILTER --> DEDUP["Dedup por place_id<br/>(fallback nombre+dirección+coords)"]
    DEDUP --> MONGO[("Mongo negocios<br/>upsert + location GeoJSON + 2dsphere")]
    DEDUP --> CHK["Checkpoint cells.jsonl<br/>clave keyword|cell_id"]
    CHK --> Q
    Q -- no --> REP["report.json + cells.csv<br/>celdas procesadas/subdivididas/descartadas"]
    SC -- "pausa aleatoria 3-8 s" --> Q
```

**Calibración empírica** (2026-10-11, bbox mancha urbana SCZ):

| Parámetro | Valor encontrado |
|---|---|
| Tope de resultados por búsqueda | ~**120** con `end of list` **falso** en celdas densas |
| Zoom que resuelve saturación | **17** (z16=120 → z17=61/41) |
| Zoom a evitar | 18–19: la celda densa sigue ≥120 y la subdivisión **explota** |
| `review_count` | queda como **string** → ordenar con `$toInt` |

---

## 3. Scrape de reseñas — `start.py scrape`

Por cada negocio del `config.yaml` el scraper sigue este flujo; el repositorio de terceros
trajo **4 bugs** que se corrigieron para que funcione (ver §5).

```mermaid
flowchart TD
    C["config.yaml"] --> DRV["setup_driver<br/>Chrome uc headless + cookies de sesión"]
    DRV --> SMOKE["Warm-up google.com<br/>(establecer sesión/cookies)"]
    SMOKE --> NAV["navigate_to_place<br/>bypass 'vista limitada'<br/>url = maps/search/&lt;nombre&gt;/@lat,lng,17z<br/>+ fallback url directa"]
    NAV --> REG["upsert_place + start_session<br/>place_id '0x…:0' · lat/lng en SQLite"]
    REG --> TAB["click_reviews_tab<br/>fast-path: clickea [role=tab]<br/>por texto 'Reseñas'/'Opiniones'…"]
    TAB --> PANE["Buscar pane de reseñas<br/>refinar al de más div[data-review-id]<br/>(evita pane de resumen con 5 cards)"]
    PANE --> I{"Iteración scroll + parse"}
    I --> WHEEL["_wheel_scroll<br/>CDP Input.dispatchMouseEvent mouseWheel<br/>(dispara lazy-load de reseñas)"]
    WHEEL --> CARDS["Leer div[data-review-id]<br/>author · rating · texto · fecha · likes"]
    CARDS --> MERGE["Fusionar con SQLite<br/>review_id dedup · batch new/updated"]
    MERGE --> SAVE["PostScrapeRunner<br/>fechas (DateTask) · imágenes · JSON backup<br/>Mongo upsert + merge custom_params"]
    SAVE --> I
    I -- "idle/end" --> NEXT["siguiente negocio"]
    NEXT --> DRV
```

Puntos de fallo conocidos (por eso existen los fixes del repo):
- **Nombre/etiqueta de pestaña** varía entre locales (Reseñas / Opiniones / Reviews…) → click por texto.
- **Lazy-load** solo responde al evento nativo `mouseWheel` (el `scrollBy` JS quedaba en 3 reseñas).
- **Pane equivocado**: el selector primario podía matchear el pane de resumen (~5 reseñas) → refinar al pane con más tarjetas.

---

## 4. Enriquecimiento — `enrich_mongo.py`

Google ni arma GeoJSON ni publica emails; se enriquecen las reseñas ya guardadas.

```mermaid
flowchart LR
    M[(("Mongo reseñas<br/>dental: 739 docs"))] --> J["Join por prefijo hex del place_id<br/>(mongo '0x…:0' ↔ discovery '0x…:0x…')"]
    DI[("discovery.json<br/>lat/lng · url")] --> J
    J --> LOC["$set por place_id<br/>location = {type:'Point', coordinates:[lng, lat]}<br/>+ lat/lng + índice 2dsphere"]
    WEB["Crawl del sitio web<br/>home + páginas de contacto<br/>mailto:+regex + unescape HTML<br/>descarta emails de plataformas"] --> EMI["website · email (uid)"]
    EMI --> MO[(("Mongo enriquecido"))]
    LOC --> MO
```

---

## 5. Modelo de datos en Mongo

```mermaid
erDiagram
    NEGOCIOS ||..o{ RESENAS : "misma empresa (prefijo hex)"
    NEGOCIOS {
        string place_id PK "0x..:0x.. (discovery)"
        string name
        string category
        string address
        string phone
        string website
        string lat
        string lng
        GeoJSON location "Point [lng, lat]"
        string found_keyword "dental / odontología…"
        string found_cell "z16_.._.."
    }
    RESENAS {
        string review_id PK
        string place_id "0x..:0 (scraper)"
        string author
        string author_profile_url
        int rating "1-5"
        string description
        date review_date
        int likes
        string company
        string address
        string phone
        string category
        GeoJSON location
        string website
        string email
        array sub_ratings
        array user_images
        array owner_responses
    }
```

> Nota del join: el scraper guarda `place_id` como `0x<hex>:0` y el discovery como
> `0x<hex>:0x<cid>`; se unen por el prefijo `<hex>` (antes de `:`).

---

## 6. Métodos de uso (comandos)

Todos desde el raíz del workspace con el venv aislado:

### 6.1 Descubrimiento

```bash
# Modo simple (zona por texto)
googleReviews/.venv/bin/python googleReviews/scripts/discover.py \
  --keyword "dental" --location "Santa Cruz de la Sierra" \
  --out-dir googleReviews/scripts/dental_scz
```

```bash
# Modo cobertura completa (rastrillaje)
googleReviews/.venv/bin/python googleReviews/scripts/rasterize.py \
  --bbox "-17.87,-63.22,-17.74,-63.13" \        # o --area "…" (geocodifica)
  --keywords "dental,odontología" \
  --zoom 16 --zoom-max 17 --overlap 0.15 \
  --out-dir googleReviews/scripts/raster/dental_urbano \
  --mongo-collection negocios                    # upsert a Mongo local

# Revisar la cuadrícula SIN pegarle a Google
... --dry-run
```

- Checkpoint automático en `<out-dir>/cells.jsonl` (reanudar = repetir el mismo comando).
- Recomendado `--zoom-max 17` (evita la explosión de subdivisiones, §2.2).
- Salidas: `discovery.json`·`csv`, `report.json`, `cells.jsonl`·`cells.csv`.

### 6.2 Generar config del scraper

```bash
googleReviews/.venv/bin/python googleReviews/scripts/build_config.py \
  --discovery googleReviews/scripts/raster/dental_urbano/discovery.json \
  --out googleReviews/scripts/raster/dental_urbano/config.yaml \
  --keyword "dental" --mongo-collection dental
```

```yaml
# settings clave (config.yaml)
sort_by: relevance          # evita clics del menú de orden (roto en esta UI)
scroll_idle_limit: 5
max_scroll_attempts: 200
mongodb: { database: google_reviews, collection: dental, sync_mode: update }
```

### 6.3 Scrapear reseñas

```bash
cd googleReviews && .venv/bin/python start.py \
  --config "$PWD/../googleReviews/scripts/raster/dental_urbano/config.yaml"
```

Comandos útiles de `start.py`: `health` (probe sintético), `db-stats`, `export`,
`sync-status`, `clear`, `migrate`.

### 6.4 Enriquecer

```bash
googleReviews/.venv/bin/python googleReviews/scripts/enrich_mongo.py \
  --discovery googleReviews/scripts/raster/dental_urbano/discovery.json \
  --db google_reviews --collection dental --crawl-emails
```

### 6.5 Verificación

```bash
mongosh "mongodb://admin:admin@127.0.0.1:27017/?authSource=admin" --quiet --eval '
  db.getSiblingDB("google_reviews").negocios.countDocuments();      # 929
  db.getSiblingDB("google_reviews").dental.countDocuments();        # 739 (rescadas)
'
```

---

## 7. Bugs corregidos en el repo de terceros (parches locales)

| # | Archivo | Problema | Fix |
|---|---|---|---|
| 1 | `modules/data_storage.py` | pymongo 4.12 interpreta cualquier opción `tls*` como TLS → `SSL handshake failed` | pasar la opción solo si `True` |
| 2 | `modules/scraper.py::click_reviews_tab` | etiqueta de pestaña varía (Reseñas/Opiniones/…) → caía en vista de 3 reseñas | fast-path: click por `text`/`aria-label` con keywords multilenguaje |
| 3 | `modules/scraper.py::_wheel_scroll` | scroll JS no dispara el lazy-load (3 reseñas) | evento nativo CDP `Input.dispatchMouseEvent mouseWheel` sobre el pane |
| 4 | `modules/scraper.py` (selección de pane) | el selector primario podía caer en el pane de resumen (~5 reviews) | refinar por JS al pane con **más** `div[data-review-id]` |

---

## 8. Resultados de la corrida de referencia (2026-10-11)

- **`negocios`:** 929 (dental 657, odontología 272) · 929 con `location` · 153 website · 588 teléfono.
- **`dental` (reseñas):** 739 · smoke Odontoking 312/312 · 28 negocios sin reseñas (legítimo).
- Dato de cobertura: búsqueda de ciudad = 98 únicos; solo 1 celda del centro (1 palabra) = 233 → **el rastrillaje multiplica la cobertura**.