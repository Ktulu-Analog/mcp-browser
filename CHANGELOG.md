# Changelog

## [Unreleased]

### Ajouté

- **11e outil `browser_chercher_images`** — recherche d'images multi-sources (Wikimedia Commons API, OpenVerse, Bing Images headless) avec licences incluses dans les résultats.
- **Moteur SearXNG** — 4e moteur de recherche disponible, avec fallback automatique dans la chaîne `duckduckgo → bing → google → searxng`.
- **Compression automatique des screenshots** — redimensionnement 900 px max et compression JPEG progressive par paliers (70/55/40/25/15) pour rester sous 60 Ko (~90k tokens contexte LLM). Nécessite Pillow.
- **Détection des challenges Cloudflare et WAF** — patterns CF IUAM/Turnstile, DataDome, Imperva, PerimeterX, Akamai ; attente automatique de résolution JS (~5s).
- **Fallback FlareSolverr** — en dernier recours, délégation à un service Chromium headful sous Xvfb. Configurable via `FLARESOLVERR_URL`.
- **Persistance des cookies CF** — les cookies `cf_clearance` obtenus via FlareSolverr sont sauvegardés dans `COOKIE_FILE` (défaut `/data/cookies.json`) et rechargés au démarrage.
- **Gestion RGPD / consent walls** — acceptation automatique OneTrust, Didomi, SourcePoint (Le Figaro, Le Parisien), CookieBot, Quantcast, Google Consent Mode v2 (flow 2 étapes + iframe cross-origin), Yahoo/Oath, boutons génériques fr/en.
- **Détection paywall** — analyse CSS + textuelle (fr/en) avant l'attente d'hydratation JS ; erreur descriptive immédiate si paywall confirmé.
- **Attente hydratation SPA / JS-only** — 14 sélecteurs sémantiques ordonnés, timeout global 15s, fallback fixe 3s. Support Yahoo Finance, météo, news.
- **Support requêtes `site:`** — extraction du domaine, filtrage des résultats, fallback navigation directe avec moteurs internes connus (Légifrance, Le Monde, Le Figaro, Wikipedia, GitHub…) et patterns génériques.
- **Pré-chauffe Chromium au démarrage** — lifespan FastMCP lance Chromium à l'init pour éviter le cold-start sur la 1ère requête MCP.
- **Headers `Sec-CH-UA` cohérents** — générés dynamiquement selon l'UA sélectionné (Chrome 136/137, Edge 136 ; absent pour Firefox).
- **User-Agents 2026** — liste mise à jour Chrome 136/137, Firefox 137, Edge 136.
- **playwright-stealth v2** — détection et hook automatique si le paquet est installé (`pip install playwright-stealth`).
- **Script stealth manuel étendu** — 12 neutralisations JS : webdriver, plugins, langues, hardwareConcurrency, deviceMemory, window.chrome complet (runtime/csi/loadTimes), permissions, WebGL vendor/renderer, Error.stack headless, ChromeDriver markers ($cdc_/$wdc_), outerWidth/Height, screen.colorDepth, navigator.connection.
- **Blocage ressources inutiles** — polices web et domaines tracking/pub bloqués via `page.route` (Google Analytics, DoubleClick, Facebook, Taboola, Outbrain…).
- **Volume Docker nommé `mcp-browser-data`** — persistance des cookies CF entre `docker compose down/up`.
- **`docker-compose.yml` avec FlareSolverr** — service flaresolverr avec healthcheck, dépendance conditionnelle.

### Modifié

- Port par défaut changé de `6502` à `6503`.
- `browser_screenshot` retourne désormais un bloc `ImageContent` MCP natif (type=image) pour affichage direct dans le chat, au lieu d'un bloc base64 inline dans du texte.
- `_extract_markdown` nettoie désormais aussi `[class*="pub"]`, `[class*="ad-"]`, `[id*="ad"]`, `iframe`, `noscript` en plus des sélecteurs existants.
- `docker-compose.yml` mis à jour : port 6503, variable `FLARESOLVERR_URL`, volume `/data`.
- `Dockerfile` : port exposé mis à jour (6503), `EXPOSE 6502` → `EXPOSE 6503`.
- `.env.example` : ajout des variables `FLARESOLVERR_URL` et `COOKIE_FILE`.
- `.gitignore` : ajout de `data/` et `cookies.json`.
