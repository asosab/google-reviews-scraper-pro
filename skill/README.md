# googleReviews — skill opencode (operación del pipeline)

Skill de **opencode** para operar el pipeline **Google Reviews** (negocios + reseñas de
Google Maps → MongoDB). Vive dentro del propio repo `googleReviews/`, junto a la app y a
los scripts, de modo que **un solo clon + commit comparte todo** (app + scripts + skill +
parches).

## Contenido

```
googleReviews/
├── (app de terceros: start.py, modules/, README.md, ...)
├── service.sh            ← arranque/parada de procesos (único punto, ver AGENTS.md)
├── scripts/              ← scripts propios (descubrimiento/enriquecimiento/cierre)
│   ├── investigar.py     ←   orquestador: un comando lanza F1→F6 (por service.sh)
│   ├── rasterize.py      ←   rastrillaje por cuadrícula (cobertura completa)
│   ├── discover.py       ←   búsqueda por texto (sondeo rápido)
│   ├── build_config.py   ←   genera config.yaml para el scraper
│   ├── enrich_mongo.py   ←   GeoJSON + website + email sobre Mongo
│   ├── sync_negocio_reviews.py ←  reseñas → negocio_reviews (con negocio_id)
│   └── investigaciones.py ←  negocios_investigacion + investigacion_ID
└── skill/                ← este skill
    ├── SKILL.md          ←   skill opencode (operativa + calibración probada)
    ├── README.md         ←   este archivo
    ├── patches/          ←   parches que arreglan los bugs del repo de terceros
    └── docs/INFORME.md   ←   informe con diagramas Mermaid
```

## Prerrequisitos en la máquina destino

- Clone del repo `googleReviews/` (app de terceros v1.2.3 + nuestros cambios) con venv
  creado e instalado su `requirements.txt`.
- MongoDB local accesible (por defecto `mongodb://admin:admin@127.0.0.1:27017/`,
  sobrescribible por argumento).
- Chrome + driver (bundled por seleniumbase; si falla: `seleniumbase install chromedriver`).

## Instalación del skill en otro opencode

1. Instalar `googleReviews/skill/SKILL.md` en cualquiera de estas rutas:

   - **Proyecto (recomendado):** `<tu-workspace>/.opencode/skills/google-reviews-scraper/SKILL.md`
   - **Global (todas tus sesiones):** `~/.config/opencode/skills/google-reviews-scraper/SKILL.md`
   - **Vía config:** apuntar al directorio del skill dentro del repo clonado:
     ```json
     { "skills": { "paths": ["ruta/a/googleReviews/skill"] } }
     ```

2. **Reiniciar opencode** (los skills se cargan al arranque).
3. Verificar: `opencode /skills` (o el selector de skills de la UI) debe listar
   `google-reviews-scraper`.

## Aplicar los parches (solo si se clona el repo de terceros de cero)

```bash
cd googleReviews && git apply \
  skill/patches/01-data_storage-tls.patch \
  skill/patches/02-scraper-fixes.patch
```

Sin estos parches el scraper falla: error TLS de pymongo, ≤5 reseñas por negocio
(pane/solo-3/tab erróneos). Detalles en `SKILL.md` (Fase 3).

## Uso

Con el skill instalado, basta pedirle a cualquier agente:

> "Scrapea con googleReviews los negocios de <NICHO> en <CIUDAD>, guarda en Mongo local
> y genera el config para scrapear reseñas."

El agente sigue el orden obligatorio F1→F7 del `SKILL.md` (dry-run primero, smoke luego,
corrida final), si un paso de mínima validación falla, se detiene y reporta.

### Todo el pipeline en un solo comando

`scripts/investigar.py` encadena F1→F6 para una investigación nueva o existente. Se lanza
por `service.sh`:

```bash
googleReviews/service.sh investigacion --nombre "Inmobiliarias SCZ" \
    --keywords "inmobiliaria,agencia inmobiliaria" \
    --localidad "Santa Cruz de la Sierra, Bolivia" --radio 15
```

Ver "Arranque en un solo comando" en `SKILL.md` (geometrías, nueva/existente, colecciones,
límites de smoke y auto-parada).

## Referencia rápida

```bash
# F1 descubrimiento (cobertura completa, resumible)
googleReviews/.venv/bin/python googleReviews/scripts/rasterize.py \
  --bbox "-17.87,-63.22,-17.74,-63.13" --keywords "dental,odontología" \
  --zoom 16 --zoom-max 17 --overlap 0.15 \
  --out-dir googleReviews/scripts/raster/<caso> --mongo-collection negocios --dry-run

# F2 config
googleReviews/.venv/bin/python googleReviews/scripts/build_config.py \
  --discovery googleReviews/scripts/raster/<caso>/discovery.json \
  --out googleReviews/scripts/raster/<caso>/config.yaml --keyword "dental" --mongo-collection dental

# F3 scrape
(cd googleReviews && .venv/bin/python start.py --config "$PWD/../googleReviews/scripts/raster/<caso>/config.yaml")

# F4 enriquecimiento
googleReviews/.venv/bin/python googleReviews/scripts/enrich_mongo.py \
  --discovery googleReviews/scripts/raster/<caso>/discovery.json \
  --db google_reviews --collection dental --crawl-emails

# F5 sincronización de reseñas → negocio_reviews
googleReviews/.venv/bin/python googleReviews/scripts/sync_negocio_reviews.py \
  --collections dental,smoke

# F6 registro de investigación
googleReviews/.venv/bin/python googleReviews/scripts/investigaciones.py \
  --new --nombre "dental SCZ" --keywords "dental,odontología" \
  --bbox "-17.87,-63.22,-17.74,-63.13" --duracion-seg 16800 --tag all

# F7 verificación
mongosh "mongodb://admin:admin@127.0.0.1:27017/?authSource=admin"
```
