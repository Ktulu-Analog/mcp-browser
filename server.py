#!/usr/bin/env python3
# ============================================================================
# BROWSER — Serveur MCP HTTP Streamable (spec 2025-03-26)
# ============================================================================
# Auteur  : Pierre COUGET  ktulu.analog@gmail.com
# Licence : GNU Affero General Public License v3.0 (AGPL-3.0)
# Année   : 2026
# ============================================================================

import asyncio
import base64
import json
import logging
import os
import random
import re
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compression d'image — réduit les screenshots pour tenir dans le contexte LLM
# ---------------------------------------------------------------------------
_MAX_SCREENSHOT_PIXELS = 900    # largeur max en px
_MAX_SCREENSHOT_KB     = 60     # taille max JPEG en Ko (~90k tokens dans le contexte LLM)


def _compress_screenshot(png_bytes: bytes, max_width: int = _MAX_SCREENSHOT_PIXELS, max_kb: int = _MAX_SCREENSHOT_KB) -> bytes:
    """Redimensionne et compresse un PNG pour qu'il tienne dans le contexte LLM."""
    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")

        w, h = img.size
        max_height = max_width * 3
        if w > max_width or h > max_height:
            ratio = min(max_width / w, max_height / h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

        quality = 70
        buf = io.BytesIO()
        for quality in [70, 55, 40, 25, 15]:
            buf.seek(0); buf.truncate()
            img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
            if buf.tell() <= max_kb * 1024:
                break

        compressed = buf.getvalue()
        logger.info(f"screenshot compressé : {len(png_bytes)//1024} Ko → {len(compressed)//1024} Ko (qualité {quality})")
        return compressed

    except ImportError:
        logger.warning("Pillow non installé — screenshot retourné sans compression (pip install Pillow)")
        return png_bytes
    except Exception as e:
        logger.warning(f"Compression screenshot échouée : {e} — retour original")
        return png_bytes


mcp = FastMCP(
    name="browser",
    instructions=(
        "Serveur MCP de navigation web avancée via Playwright (Chromium). "
        "Outils : recherche web multi-moteur, navigation et extraction markdown/HTML, "
        "screenshot, clic, formulaires, research multi-sources, session persistante, healthcheck."
    ),
)

# ---------------------------------------------------------------------------
# User-Agents 2026 — Chrome 136/137 stable + Firefox 137
# Mis à jour pour correspondre aux versions actuelles (mai 2026)
# ---------------------------------------------------------------------------
_USER_AGENTS = [
    # Chrome 136 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    # Chrome 136 macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    # Chrome 137 Windows (Canary/Beta)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    # Chrome 136 Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    # Firefox 137 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
    # Firefox 137 macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:137.0) Gecko/20100101 Firefox/137.0",
    # Edge 136 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
]

# Correspondance UA → sec-ch-ua headers cohérents
def _get_ch_ua_headers(ua: str) -> Dict[str, str]:
    """Retourne les Sec-CH-UA cohérents avec l'User-Agent choisi."""
    if "Chrome/137" in ua:
        return {
            "Sec-CH-UA": '"Chromium";v="137", "Google Chrome";v="137", "Not/A)Brand";v="99"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"' if "Windows" in ua else ('"macOS"' if "Macintosh" in ua else '"Linux"'),
        }
    elif "Chrome/136" in ua or "Edg/136" in ua:
        brand = '"Microsoft Edge";v="136"' if "Edg" in ua else '"Google Chrome";v="136"'
        return {
            "Sec-CH-UA": f'"Chromium";v="136", {brand}, "Not/A)Brand";v="99"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"' if "Windows" in ua else ('"macOS"' if "Macintosh" in ua else '"Linux"'),
        }
    else:
        # Firefox — pas de Sec-CH-UA
        return {}

# ---------------------------------------------------------------------------
# Moteurs de recherche — URLs et extracteurs
# ---------------------------------------------------------------------------
_SEARCH_STRATEGIES = {
    "duckduckgo": [
        {
            "url": "https://html.duckduckgo.com/html/?q={q}&kl=fr-fr",
            "wait": "domcontentloaded",
            "js": """(maxN) => Array.from(document.querySelectorAll(
                        '.result, .results_links, [class*="result"]'))
                    .filter(el => el.querySelector('a[href^="http"]'))
                    .slice(0, maxN)
                    .map(el => {
                        const links = el.querySelectorAll('a[href^="http"]');
                        const a = Array.from(links).find(l =>
                            !l.href.includes('duckduckgo') && !l.href.includes('duck.co')
                        ) || links[0];
                        const titleEl = el.querySelector('h2, .result__title, [class*="title"]');
                        const snippetEl = el.querySelector('.result__snippet, [class*="snippet"], p');
                        return a ? {
                            title: (titleEl || a).textContent.trim().slice(0,120),
                            url: a.href,
                            snippet: snippetEl ? snippetEl.textContent.trim().slice(0,250) : ''
                        } : null;
                    }).filter(r => r && r.url && !r.url.includes('duckduckgo'))"""
        },
        {
            "url": "https://html.duckduckgo.com/html/?q={q}",
            "wait": "domcontentloaded",
            "js": """(maxN) => {
                const seen = new Set();
                return Array.from(document.querySelectorAll('a[href^="http"]'))
                    .filter(a => {
                        const h = a.href;
                        return !h.includes('duckduckgo') && !h.includes('duck.co')
                            && !h.includes('javascript') && h.length > 10 && !seen.has(h)
                            && (seen.add(h) || true);
                    })
                    .slice(0, maxN)
                    .map(a => ({
                        title: a.textContent.trim().slice(0,120) || a.href.slice(0,80),
                        url: a.href,
                        snippet: ''
                    }));
            }"""
        },
    ],
    "bing": [
        {
            "url": "https://www.bing.com/search?q={q}&setlang=fr&cc=FR&count=15",
            "wait": "domcontentloaded",
            "js": """(maxN) => Array.from(document.querySelectorAll(
                        'li.b_algo, .b_algo, #b_results > li'))
                    .slice(0, maxN)
                    .map(el => {
                        const a = el.querySelector('h2 a, h3 a, .b_title a');
                        const snip = el.querySelector('.b_caption p, .b_paractl, p');
                        if (!a) return null;
                        // Préférer data-href (URL réelle) à href (URL de redirection bing.com/ck/a)
                        const raw = a.getAttribute('data-href') || a.getAttribute('href') || a.href || '';
                        // Déréférencer manuellement les URLs bing.com/ck/a?...&u=a1...
                        let url = raw;
                        if (url.includes('bing.com/ck/') || url.includes('bing.com/aclick')) {
                            try {
                                const u = new URL(url.startsWith('http') ? url : 'https://bing.com' + url);
                                const dest = u.searchParams.get('u') || u.searchParams.get('url');
                                if (dest) {
                                    // Les params bing encodent parfois en base64 partiel : a1aHR0c...
                                    const cleaned = dest.startsWith('a1') ? dest.slice(2) : dest;
                                    try { url = atob(cleaned); } catch(e) { url = dest; }
                                }
                            } catch(e) {}
                        }
                        return url.startsWith('http') ? {
                            title: a.textContent.trim().slice(0,120),
                            url: url,
                            snippet: snip ? snip.textContent.trim().slice(0,250) : ''
                        } : null;
                    }).filter(r => r && r.url)"""
        },
    ],
    "google": [
        {
            "url": "https://www.google.com/search?q={q}&num=10&hl=fr&gl=FR",
            "wait": "domcontentloaded",
            "js": """(maxN) => Array.from(document.querySelectorAll('div.g, .g'))
                    .slice(0, maxN)
                    .map(el => {
                        const a = el.querySelector('a[href]');
                        const h3 = el.querySelector('h3');
                        const snip = el.querySelector('.VwiC3b, [data-sncf], .st');
                        return (a && h3) ? {
                            title: h3.textContent.trim().slice(0,120),
                            url: a.href,
                            snippet: snip ? snip.textContent.trim().slice(0,250) : ''
                        } : null;
                    }).filter(r => r && r.url && r.url.startsWith('http'))"""
        },
    ],
    "searxng": [
        {
            "url": "https://searx.be/search?q={q}&language=fr",
            "wait": "domcontentloaded",
            "js": """(maxN) => Array.from(document.querySelectorAll('.result, article'))
                    .slice(0, maxN)
                    .map(el => {
                        const a = el.querySelector('h3 a, h4 a, a.result_title');
                        const snip = el.querySelector('p.content, .result-content');
                        return a ? {
                            title: a.textContent.trim().slice(0,120),
                            url: a.href,
                            snippet: snip ? snip.textContent.trim().slice(0,250) : ''
                        } : null;
                    }).filter(r => r && r.url && r.url.startsWith('http'))"""
        },
    ],
}

_ENGINE_FALLBACK = ["duckduckgo", "bing", "google", "searxng"]

# ---------------------------------------------------------------------------
# Script d'initialisation stealth — injecté avant chaque page
# Neutralise les dizaines de signaux utilisés par Cloudflare Bot Management
# ---------------------------------------------------------------------------
_STEALTH_INIT_SCRIPT = """
() => {
    // ── 1. Masquer webdriver ────────────────────────────────────────────────
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
    delete navigator.__proto__.webdriver;

    // ── 2. Plugins réalistes (Chrome en a 3 par défaut) ────────────────────
    const makePlugin = (name, filename, desc, mimeType, mimeDesc, suffix) => {
        const mime = Object.create(MimeType.prototype);
        Object.defineProperties(mime, {
            type: { get: () => mimeType },
            description: { get: () => mimeDesc },
            suffixes: { get: () => suffix },
        });
        const plugin = Object.create(Plugin.prototype);
        Object.defineProperties(plugin, {
            name: { get: () => name },
            filename: { get: () => filename },
            description: { get: () => desc },
            length: { get: () => 1 },
            0: { get: () => mime },
        });
        mime.__defineGetter__('enabledPlugin', () => plugin);
        return plugin;
    };

    const fakePlugins = [
        makePlugin('Chrome PDF Plugin','internal-pdf-viewer','Portable Document Format','application/x-google-chrome-pdf','Portable Document Format','pdf'),
        makePlugin('Chrome PDF Viewer','mhjfbmdgcfjbbpaeojofohoefgiehjai','','application/pdf','Portable Document Format','pdf'),
        makePlugin('Native Client','internal-nacl-plugin','','application/x-nacl','Native Client Executable','nexe'),
    ];

    const pluginArray = Object.create(PluginArray.prototype);
    fakePlugins.forEach((p, i) => { pluginArray[i] = p; });
    Object.defineProperties(pluginArray, {
        length: { get: () => fakePlugins.length },
        item: { value: i => fakePlugins[i] },
        namedItem: { value: name => fakePlugins.find(p => p.name === name) || null },
        refresh: { value: () => {} },
    });
    Object.defineProperty(navigator, 'plugins', { get: () => pluginArray, configurable: true });
    Object.defineProperty(navigator, 'mimeTypes', {
        get: () => {
            const arr = Object.create(MimeTypeArray.prototype);
            arr[0] = pluginArray[0][0]; arr[1] = pluginArray[1][0];
            Object.defineProperty(arr, 'length', { get: () => 2 });
            return arr;
        }, configurable: true
    });

    // ── 3. Langues réalistes ───────────────────────────────────────────────
    Object.defineProperty(navigator, 'languages', { get: () => ['fr-FR', 'fr', 'en-US', 'en'], configurable: true });

    // ── 4. Hardware concurrency réaliste ──────────────────────────────────
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8, configurable: true });
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8, configurable: true });

    // ── 5. Chrome runtime object complet ──────────────────────────────────
    if (!window.chrome) {
        window.chrome = {};
    }
    window.chrome.app = {
        isInstalled: false,
        InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
        RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
        getDetails: () => null,
        getIsInstalled: () => false,
        installState: cb => cb('not_installed'),
        runningState: () => 'cannot_run',
    };
    window.chrome.runtime = {
        OnInstalledReason: { CHROME_UPDATE:'chrome_update', INSTALL:'install', SHARED_MODULE_UPDATE:'shared_module_update', UPDATE:'update' },
        OnRestartRequiredReason: { APP_UPDATE:'app_update', OS_UPDATE:'os_update', PERIODIC:'periodic' },
        PlatformArch: { ARM:'arm', ARM64:'arm64', MIPS:'mips', MIPS64:'mips64', X86_32:'x86-32', X86_64:'x86-64' },
        PlatformNaclArch: { ARM:'arm', MIPS:'mips', MIPS64:'mips64', X86_32:'x86-32', X86_64:'x86-64' },
        PlatformOs: { ANDROID:'android', CROS:'cros', LINUX:'linux', MAC:'mac', OPENBSD:'openbsd', WIN:'win' },
        RequestUpdateCheckStatus: { NO_UPDATE:'no_update', THROTTLED:'throttled', UPDATE_AVAILABLE:'update_available' },
        connect: () => {},
        sendMessage: () => {},
    };
    window.chrome.csi = () => ({ startE: Date.now(), onloadT: Date.now() + 100, pageT: 1000 + Math.random() * 500, tran: 15 });
    window.chrome.loadTimes = () => ({
        commitLoadTime: Date.now() / 1000 - 0.5,
        connectionInfo: 'h2',
        finishDocumentLoadTime: Date.now() / 1000 - 0.1,
        finishLoadTime: Date.now() / 1000 - 0.05,
        firstPaintAfterLoadTime: 0,
        firstPaintTime: Date.now() / 1000 - 0.3,
        navigationType: 'Other',
        npnNegotiatedProtocol: 'h2',
        requestTime: Date.now() / 1000 - 0.8,
        startLoadTime: Date.now() / 1000 - 0.8,
        wasAlternateProtocolAvailable: false,
        wasFetchedViaSpdy: true,
        wasNpnNegotiated: true,
    });

    // ── 6. Permissions réalistes ──────────────────────────────────────────
    const _origQuery = window.navigator.permissions.query.bind(navigator.permissions);
    window.navigator.permissions.query = (params) => {
        if (params.name === 'notifications') {
            return Promise.resolve({ state: Notification.permission, onchange: null });
        }
        return _origQuery(params);
    };

    // ── 7. WebGL — vendor/renderer réalistes ──────────────────────────────
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Intel Inc.';      // UNMASKED_VENDOR_WEBGL
        if (param === 37446) return 'Intel(R) UHD Graphics 620'; // UNMASKED_RENDERER_WEBGL
        return getParam.call(this, param);
    };
    const getParam2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Intel Inc.';
        if (param === 37446) return 'Intel(R) UHD Graphics 620';
        return getParam2.call(this, param);
    };

    // ── 8. Cacher les sources d'erreur Headless ────────────────────────────
    // Neutralise la détection via Error.stack ("HeadlessChrome")
    const _origError = Error;
    window.Error = function(...args) {
        const err = new _origError(...args);
        const stack = err.stack || '';
        if (stack.includes('HeadlessChrome') || stack.includes('headlesschrome')) {
            err.stack = stack.replace(/HeadlessChrome/gi, 'Chrome');
        }
        return err;
    };
    Object.assign(window.Error, _origError);

    // ── 9. Masquer les propriétés automation CDP ───────────────────────────
    // Supprime $cdc_ et $wdc_ (ChromeDriver markers)
    ['$cdc_asdjflasutopfhvcZLmcfl_', '$wdc_'].forEach(k => {
        try { delete window[k]; } catch(e) {}
    });

    // ── 10. outerWidth / outerHeight cohérents avec viewport ───────────────
    if (window.outerWidth === 0) {
        Object.defineProperty(window, 'outerWidth', { get: () => window.innerWidth + 17 });
        Object.defineProperty(window, 'outerHeight', { get: () => window.innerHeight + 90 });
    }

    // ── 11. screen.colorDepth réaliste ────────────────────────────────────
    Object.defineProperty(screen, 'colorDepth', { get: () => 24, configurable: true });
    Object.defineProperty(screen, 'pixelDepth', { get: () => 24, configurable: true });

    // ── 12. navigator.connection réaliste ─────────────────────────────────
    if (!navigator.connection) {
        Object.defineProperty(navigator, 'connection', {
            get: () => ({ downlink: 10, effectiveType: '4g', rtt: 50, saveData: false }),
            configurable: true,
        });
    }
}
"""

# ---------------------------------------------------------------------------
# Détection des challenges Cloudflare / WAF
# ---------------------------------------------------------------------------
_CF_CHALLENGE_PATTERNS = [
    # Cloudflare Turnstile / IUAM
    r"challenge-platform",
    r"cf-chl-bypass",
    r"Just a moment\.\.\.",
    r"Checking if the site connection is secure",
    r"DDoS protection by Cloudflare",
    r"Enable JavaScript and cookies to continue",
    r"cf\.challenge",
    # DataDome
    r"datadome",
    r"dd_referrer",
    # Imperva / Incapsula
    r"_Incapsula_Resource",
    r"visitorId=",
    # PerimeterX
    r"PerimeterX",
    r"px-captcha",
    # Akamai
    r"ak_bmsc",
]

_CF_CHALLENGE_RE = re.compile("|".join(_CF_CHALLENGE_PATTERNS), re.I)


async def _is_cf_challenge(page) -> bool:
    """Détecte si la page affiche un challenge Cloudflare ou WAF similaire."""
    try:
        title = await page.title()
        if any(p in title for p in ["Just a moment", "Access denied", "Attention Required"]):
            return True
        html = await page.content()
        return bool(_CF_CHALLENGE_RE.search(html[:4000]))
    except Exception:
        return False


async def _wait_cf_challenge(page, max_wait_s: int = 15) -> bool:
    """
    Attend que le challenge Cloudflare se résolve automatiquement.
    Cloudflare IUAM se résout en ~5s si JS est activé.
    Retourne True si la page est passée, False si timeout.
    """
    logger.info("Challenge Cloudflare détecté — attente résolution automatique...")
    for _ in range(max_wait_s):
        await asyncio.sleep(1)
        if not await _is_cf_challenge(page):
            logger.info("Challenge Cloudflare résolu ✓")
            return True
    logger.warning(f"Challenge Cloudflare non résolu après {max_wait_s}s")
    return False


# ---------------------------------------------------------------------------
# Détection paywall / accès restreint
# ---------------------------------------------------------------------------

_PAYWALL_HINTS = [
    # Français
    "abonnez-vous", "s'abonner", "abonnement", "accès réservé",
    "réservé aux abonnés", "contenu réservé", "article réservé",
    "pour lire la suite", "lire la suite",
    "offre d'abonnement", "premium",
    # Anglais
    "subscribe", "subscription required", "subscribers only",
    "premium content", "member only", "sign in to read",
    "to continue reading", "create an account",
    # Paywalls connus
    "piano-id", "lepaywall", "wall-content",
    "offer-wall", "paywall",
]

_PAYWALL_SELECTORS = [
    "[class*='paywall']",
    "[id*='paywall']",
    "[class*='subscribe-wall']",
    "[class*='offer-wall']",
    ".piano-offer-overlay",
    "#piano-id",
    "[class*='premium-wall']",
    "[class*='subscription-wall']",
]


async def _detect_paywall(page) -> str:
    """
    Détecte si la page est bloquée par un paywall.
    Retourne un message d'erreur descriptif ou "" si pas de paywall.
    """
    try:
        # 1. Sélecteurs CSS spécifiques aux paywalls
        for sel in _PAYWALL_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=300):
                    return f"Contenu réservé aux abonnés (paywall détecté : {sel})"
            except Exception:
                continue

        # 2. Vérification textuelle dans le HTML
        snippet = (await page.content())[:8000].lower()
        matches = [h for h in _PAYWALL_HINTS if h in snippet]
        if len(matches) >= 2:  # Au moins 2 indices pour éviter les faux positifs
            return f"Contenu probablement réservé aux abonnés ({', '.join(matches[:3])})"

        return ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Détection et attente contenu JS-only (SPA React/Vue/Angular)
# ---------------------------------------------------------------------------

# Sélecteurs de contenu principal — ordonnés du plus sémantique au plus générique
_CONTENT_SELECTORS = [
    "main",
    "article",
    "[role='main']",
    "#content",
    "#main-content",
    ".main-content",
    # Finance / données
    "[data-testid='quote-header']",
    "[data-testid='fin-streamer']",
    # Météo
    "[class*='forecast']",
    "[class*='meteo']",
    "[class*='weather']",
    # News / articles
    ".article__body",
    ".article-content",
    "[class*='article']",
    # Fallback générique
    "p",
]


async def _wait_for_js_content(page, current_content: str, extract_format: str) -> str:
    """
    Attend que le contenu JS soit hydraté quand la page retourne < 500 chars.
    Timeout global : 15s maximum pour ne pas bloquer indéfiniment.
    """
    from playwright.async_api import TimeoutError as PWTimeout
    import time

    logger.info(f"Page JS-only détectée ({len(current_content)} chars) — attente hydratation DOM")
    deadline = time.monotonic() + 15  # 15s max au total

    for selector in _CONTENT_SELECTORS:
        if time.monotonic() >= deadline:
            break
        remaining_ms = max(500, int((deadline - time.monotonic()) * 1000))
        try:
            await page.wait_for_selector(selector, state="visible", timeout=min(3000, remaining_ms))
            await asyncio.sleep(0.5)
            if extract_format == "html":
                content = await page.content()
            elif extract_format == "text":
                content = await page.inner_text("body")
            else:
                content = await _extract_markdown_from_page(page)
            if len(content) > len(current_content):
                logger.info(f"Hydratation JS via '{selector}' — {len(content)} chars")
                return content
        except PWTimeout:
            continue
        except Exception:
            continue

    # Dernier recours : attente fixe, dans la limite du deadline
    remaining = deadline - time.monotonic()
    if remaining > 0:
        await asyncio.sleep(min(3, remaining))
    try:
        if extract_format == "html":
            content = await page.content()
        elif extract_format == "text":
            content = await page.inner_text("body")
        else:
            content = await _extract_markdown_from_page(page)
        if len(content) > len(current_content):
            logger.info(f"Hydratation JS (attente fixe 3s) — {len(content)} chars")
            return content
    except Exception:
        pass

    logger.warning("Hydratation JS échouée — contenu toujours vide après tous les sélecteurs")
    return current_content


async def _extract_markdown_from_page(page) -> str:
    """Extraction texte simple sans dépendance à self — pour appels depuis fonctions module."""
    try:
        return await page.evaluate("""() => {
            ['script','style','nav','footer','header',
             '[role="banner"]','[role="navigation"]',
             '.cookie-banner','.consent-banner','#cookie-notice'
            ].forEach(sel => {
                try { document.querySelectorAll(sel).forEach(el => el.remove()); } catch(e) {}
            });
            return document.body?.innerText?.trim() || '';
        }""")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Consent walls RGPD — détection et acceptation automatique
# Yahoo Finance, Google, Oath, Quantcast, OneTrust, Didomi, etc.
# ---------------------------------------------------------------------------

# Sélecteurs CSS ordonnés par priorité — du plus spécifique au plus générique
_CONSENT_SELECTORS = [
    # Yahoo / Oath
    'button[name="agree"]',
    'button.btn-primary[value="agree"]',
    '[data-consent-accept]',
    # Le Figaro — consent wall spécifique
    'button:has-text("ACCEPTER LES COOKIES")',
    'button:has-text("Accepter les cookies")',
    'button:has-text("Accepter et continuer")',
    # OneTrust (très répandu)
    '#onetrust-accept-btn-handler',
    '.onetrust-accept-btn-handler',
    # Didomi
    '#didomi-notice-agree-button',
    'button#didomi-notice-agree-button',
    # Quantcast / CMP génériques
    '[aria-label*="Accept"]',
    '[aria-label*="Accepter"]',
    '[aria-label*="Tout accepter"]',
    # Boutons textuels génériques (dernier recours)
    'button:has-text("Tout accepter")',
    'button:has-text("Accept all")',
    'button:has-text("Accepter tout")',
    'button:has-text("J\'accepte")',
    'button:has-text("I Accept")',
    'button:has-text("Agree")',
    'button:has-text("Continuer")',
    'button:has-text("Continue")',
]

# Sélecteurs de la 2ème étape — apparaissent après le 1er clic (ex: Google)
_CONSENT_STEP2_SELECTORS = [
    # Google consent step 2 — "Confirmer"
    'button:has-text("Confirmer")',
    'button:has-text("Confirm")',
    'button:has-text("Valider")',
    'form[action*="consent"] button[type="submit"]',
    '[jsname="b3VHJd"]',   # Google internal
    # OneTrust step 2
    '#onetrust-pc-btn-handler',
    '.save-preference-btn-handler',
    'button:has-text("Enregistrer")',
    'button:has-text("Save")',
]

_CONSENT_TITLE_HINTS = [
    "paramètres de confidentialité",
    "privacy settings",
    "before you continue",
    "avant de continuer",
    "your privacy",
    "votre vie privée",
    "vous avez choisi de refuser",
    "refuser les cookies",
    "gestion des cookies",
]

# Sélecteurs d'overlay consent sur page normale (pas de title hint nécessaire)
_CONSENT_OVERLAY_SELECTORS = [
    '#onetrust-banner-sdk',
    '#didomi-notice',
    '.didomi-popup-container',
    '[id*="cookie-banner"]',
    '[class*="cookie-banner"]',
    '[class*="consent-banner"]',
    '[id*="consent-banner"]',
    '.cmp-root',
    '#CybotCookiebotDialog',
    '[class*="cookiebot"]',
    '.qc-cmp2-container',           # Quantcast
    '#sp_message_container',        # SourcePoint / Piano (Le Figaro, Le Parisien…)
    '.sp_choice_type_ACCEPT_ALL',   # SourcePoint bouton accept
    '[class*="privacy-manager"]',
]

# Domaines connus pour ne PAS avoir de consent wall — évite les faux positifs
_NO_CONSENT_DOMAINS = [
    "wikipedia.org", "wikimedia.org", "wikidata.org",
    "github.com", "stackoverflow.com", "archive.org",
    "gouv.fr", "europa.eu",
]


async def _click_consent_selector(locator_source, selector: str, timeout_ms: int = 800) -> bool:
    """Tente de cliquer un sélecteur. Retourne True si cliqué."""
    try:
        btn = locator_source.locator(selector).first
        if await btn.is_visible(timeout=timeout_ms):
            await btn.click(timeout=2000)
            return True
    except Exception:
        pass
    return False


async def _handle_consent_wall(page, max_steps: int = 3) -> bool:
    """
    Détecte et accepte les consent walls RGPD, y compris :
    - Flows multi-étapes (Google : accepter → confirmer)
    - Iframes cross-origin (Google Consent Mode, Didomi iframe)
    - Overlays sur page normale (OneTrust, CookieBot, Quantcast)

    Retourne True si au moins un consent wall a été traité.
    """
    try:
        current_url = page.url
        if any(d in current_url for d in _NO_CONSENT_DOMAINS):
            return False

        handled_any = False

        for step in range(1, max_steps + 1):

            clicked_this_step = False

            # ── 1. Détection overlay sur page normale ─────────────────────
            # Cherche les bandeaux/modales cookie SANS vérifier le titre
            for overlay_sel in _CONSENT_OVERLAY_SELECTORS:
                try:
                    overlay = page.locator(overlay_sel).first
                    if await overlay.is_visible(timeout=400):
                        logger.info(f"Consent overlay détecté : {overlay_sel} (étape {step})")
                        # Chercher le bouton d'acceptation dans cet overlay
                        accept_sel = (_CONSENT_STEP2_SELECTORS if step > 1 else _CONSENT_SELECTORS)
                        for sel in accept_sel:
                            if await _click_consent_selector(overlay, sel):
                                logger.info(f"Consent overlay accepté : {sel} (étape {step})")
                                clicked_this_step = True
                                handled_any = True
                                break
                        if not clicked_this_step:
                            # Fallback : chercher dans la page entière
                            for sel in accept_sel:
                                if await _click_consent_selector(page, sel):
                                    logger.info(f"Consent overlay (fallback page) : {sel} (étape {step})")
                                    clicked_this_step = True
                                    handled_any = True
                                    break
                        break
                except Exception:
                    continue

            # ── 2. Détection par titre / snippet (consent wall pleine page) ─
            if not clicked_this_step:
                title = (await page.title()).lower()
                is_consent = any(h in title for h in _CONSENT_TITLE_HINTS)
                if not is_consent:
                    snippet = (await page.content())[:6000].lower()
                    is_consent = any(h in snippet for h in _CONSENT_TITLE_HINTS)

                if is_consent:
                    logger.info(f"Consent wall pleine page détecté (étape {step})")
                    selectors = _CONSENT_STEP2_SELECTORS if step > 1 else _CONSENT_SELECTORS
                    for sel in selectors:
                        if await _click_consent_selector(page, sel):
                            logger.info(f"Consent wall accepté : {sel} (étape {step})")
                            clicked_this_step = True
                            handled_any = True
                            break

            # ── 3. Iframes cross-origin (Google, Didomi) ──────────────────
            if not clicked_this_step:
                frames = page.frames
                for frame in frames[1:]:   # ignorer le frame principal
                    if frame == page.main_frame:
                        continue
                    frame_url = frame.url or ""
                    is_consent_frame = any(kw in frame_url for kw in [
                        "consent.google", "consent.youtube",
                        "didomi", "cookiebot", "onetrust",
                        "quantcast", "sourcepoint", "privacy",
                        "sp-prod", "notice.sp-prod",  # SourcePoint CDN (Le Figaro)
                    ])
                    if not is_consent_frame:
                        # Vérifier le contenu du frame
                        try:
                            frame_html = await frame.content()
                            is_consent_frame = any(h in frame_html.lower()[:3000]
                                                   for h in _CONSENT_TITLE_HINTS)
                        except Exception:
                            continue

                    if is_consent_frame:
                        logger.info(f"Consent iframe détecté : {frame_url[:60]} (étape {step})")
                        selectors = _CONSENT_STEP2_SELECTORS if step > 1 else _CONSENT_SELECTORS
                        for sel in selectors:
                            if await _click_consent_selector(frame, sel):
                                logger.info(f"Consent iframe accepté : {sel} (étape {step})")
                                clicked_this_step = True
                                handled_any = True
                                break
                        if clicked_this_step:
                            break

            if not clicked_this_step:
                # Rien trouvé à cette étape — arrêter
                if step == 1 and not handled_any:
                    pass  # silencieux si rien du tout
                elif step > 1:
                    logger.info(f"Consent wall : flow terminé après {step - 1} étape(s)")
                break

            # Attendre la transition vers l'étape suivante
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                await asyncio.sleep(1)

        if not handled_any:
            # Dernier log uniquement si quelque chose avait été détecté mais non cliqué
            pass

        return handled_any

    except Exception as e:
        logger.warning(f"_handle_consent_wall erreur : {e}")
        return False




_FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "http://flaresolverr:8191/v1")


async def _flaresolverr_get(url: str, timeout_ms: int = 60000) -> Optional[Dict[str, Any]]:
    """
    Délègue une requête GET à FlareSolverr et retourne la solution brute.
    Retourne None si FlareSolverr est absent ou a échoué.

    La réponse contient :
      solution.response   — HTML de la page après résolution CF
      solution.cookies    — cookies post-challenge (réutilisables)
      solution.userAgent  — UA utilisé par FlareSolverr
      solution.url        — URL finale après redirections
    """
    try:
        import aiohttp  # type: ignore
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _FLARESOLVERR_URL,
                json={"cmd": "request.get", "url": url, "maxTimeout": timeout_ms},
                timeout=aiohttp.ClientTimeout(total=timeout_ms / 1000 + 10),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"FlareSolverr HTTP {resp.status} pour {url[:60]}")
                    return None
                data = await resp.json()
                if data.get("status") != "ok":
                    logger.warning(f"FlareSolverr status={data.get('status')} msg={data.get('message','')[:80]}")
                    return None
                logger.info(f"FlareSolverr ✓ — challenge résolu pour {url[:60]}")
                return data.get("solution")
    except aiohttp.ClientConnectorError:
        logger.info("FlareSolverr non joignable — service absent ou non démarré")
        return None
    except Exception as e:
        logger.warning(f"FlareSolverr erreur : {e}")
        return None


# ---------------------------------------------------------------------------
# Helpers requêtes site:
# ---------------------------------------------------------------------------

import re as _re

def _extract_site_operator(query: str):
    """
    Détecte et extrait l'opérateur site: d'une requête.
    Retourne (domaine, query_sans_site) ou ("", query) si absent.

    Exemples :
      "cours bourse site:finance.yahoo.com" → ("finance.yahoo.com", "cours bourse")
      "site:legifrance.gouv.fr JO 2026"     → ("legifrance.gouv.fr", "JO 2026")
      "météo Paris"                          → ("", "météo Paris")
    """
    m = _re.search(r'\bsite:(\S+)', query, _re.IGNORECASE)
    if not m:
        return "", query
    raw_domain = m.group(1).lower()
    # Supprimer www. en tant que PRÉFIXE exact (lstrip supprime des caractères, pas un préfixe)
    domain = raw_domain[4:] if raw_domain.startswith("www.") else raw_domain
    clean = _re.sub(r'\bsite:\S+', '', query, flags=_re.IGNORECASE).strip()
    return domain, clean


# Moteurs de recherche internes connus — {domaine_partiel: pattern_url}
# {keywords} sera remplacé par la requête encodée URL
_SITE_SEARCH_ENGINES: List[tuple] = [
    ("finance.yahoo.com",    "https://finance.yahoo.com/lookup?s={keywords}"),
    ("yahoo.com",            "https://search.yahoo.com/search?p={keywords}+site%3A{domain}"),
    ("legifrance.gouv.fr",   "https://www.legifrance.gouv.fr/search/all?tab_selection=all&searchField=ALL&query={keywords}"),
    ("lemonde.fr",           "https://www.lemonde.fr/recherche/?keywords={keywords}"),
    ("lefigaro.fr",          "https://recherche.lefigaro.fr/recherche/{keywords}"),
    ("wikipedia.org",        "https://fr.wikipedia.org/w/index.php?search={keywords}"),
    ("github.com",           "https://github.com/search?q={keywords}"),
    ("stackoverflow.com",    "https://stackoverflow.com/search?q={keywords}"),
    ("meteofrance.com",      "https://meteofrance.com/recherche/{keywords}"),
]


async def _search_on_site(page, domain: str, keywords: str, nb: int) -> List[Dict]:
    """
    Fallback : tente une recherche interne sur le site cible quand les
    moteurs externes ont échoué sur une requête site:.

    1. Cherche un moteur interne connu dans _SITE_SEARCH_ENGINES
    2. Sinon tente /search?q= et /?s= (patterns communs)
    3. En dernier recours, navigue sur la page d'accueil et retourne les liens
    """
    import urllib.parse

    kw_enc = urllib.parse.quote_plus(keywords)
    base_url = f"https://{domain}"

    # 1. Moteur interne connu
    for site_pattern, url_template in _SITE_SEARCH_ENGINES:
        if site_pattern in domain or domain in site_pattern:
            url = url_template.replace("{keywords}", kw_enc).replace("{domain}", urllib.parse.quote_plus(domain))
            logger.info(f"_search_on_site moteur interne : {url[:80]}")
            results = await _navigate_and_extract_links(page, url, keywords, nb)
            if results:
                return results

    # 2. Patterns génériques courants
    for path in [f"/search?q={kw_enc}", f"/recherche?q={kw_enc}", f"/?s={kw_enc}", f"/search?query={kw_enc}"]:
        url = base_url + path
        logger.info(f"_search_on_site pattern générique : {url[:80]}")
        results = await _navigate_and_extract_links(page, url, keywords, nb)
        if results:
            return results

    # 3. Page d'accueil — retourner les liens internes comme résultats
    logger.info(f"_search_on_site fallback accueil : {base_url}")
    results = await _navigate_and_extract_links(page, base_url, keywords, nb)
    return results


async def _navigate_and_extract_links(page, url: str, keywords: str, nb: int) -> List[Dict]:
    """
    Navigue vers url et extrait les liens comme résultats de recherche.
    Filtre par pertinence avec les mots-clés si possible.
    """
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        if not resp or resp.status >= 400:
            return []

        # Attendre que le JS initial charge (consent walls, SPA)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        # Gérer le consent wall — si accepté, attendre le rechargement du contenu
        consented = await _handle_consent_wall(page)
        if consented:
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                await asyncio.sleep(2)
            # Second passage — certains sites ont un consent en 2 étapes
            await _handle_consent_wall(page)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

        links: List[Dict] = await page.evaluate("""(nb) => {
            const seen = new Set();
            return Array.from(document.querySelectorAll('a[href]'))
                .map(a => ({
                    url:     a.href,
                    title:   (a.textContent || a.title || '').trim().slice(0, 200),
                    snippet: (a.closest('li,article,div')?.textContent || '').trim().slice(0, 300),
                }))
                .filter(r => r.url.startsWith('http') && r.title.length > 5 && !seen.has(r.url) && seen.add(r.url))
                .slice(0, nb * 3);
        }""", nb)

        if not links:
            logger.debug(f"_navigate_and_extract_links({url[:60]}): aucun lien trouvé")
            return []

        # Scorer par pertinence avec les mots-clés
        kw_lower = keywords.lower().split()
        def score(r):
            text = (r["title"] + " " + r["snippet"]).lower()
            return sum(1 for k in kw_lower if k in text)

        links.sort(key=score, reverse=True)
        result = [r for r in links if r["title"]][:nb]
        logger.info(f"_navigate_and_extract_links({url[:60]}): {len(result)} liens pertinents")
        return result

    except Exception as e:
        logger.debug(f"_navigate_and_extract_links({url}): {e}")
        return []


_COOKIE_FILE = os.getenv("COOKIE_FILE", "/data/cookies.json")


def _load_persisted_cookies() -> tuple:
    """
    Charge les cookies CF et l'UA depuis le fichier de persistance.
    Retourne (cookies, user_agent) ou ([], "") si absent/invalide.
    Filtre les cookies expirés.
    """
    import time
    try:
        if not os.path.exists(_COOKIE_FILE):
            return [], ""
        with open(_COOKIE_FILE, "r") as f:
            data = json.load(f)
        now = time.time()
        cookies = [
            c for c in data.get("cookies", [])
            if not c.get("expires") or c["expires"] > now
        ]
        ua = data.get("user_agent", "")
        if cookies:
            logger.info(f"Cookies CF chargés depuis {_COOKIE_FILE} : {len(cookies)} cookies")
        return cookies, ua
    except Exception as e:
        logger.warning(f"Impossible de charger les cookies persistés : {e}")
        return [], ""


def _save_persisted_cookies(cookies: List[Dict], user_agent: str) -> None:
    """Sauvegarde les cookies CF et l'UA dans le fichier de persistance."""
    try:
        os.makedirs(os.path.dirname(_COOKIE_FILE), exist_ok=True)
        with open(_COOKIE_FILE, "w") as f:
            json.dump({"cookies": cookies, "user_agent": user_agent}, f, indent=2)
        logger.info(f"Cookies CF sauvegardés dans {_COOKIE_FILE} : {len(cookies)} cookies")
    except Exception as e:
        logger.warning(f"Impossible de sauvegarder les cookies : {e}")




class _BrowserClient:

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._session_active: bool = False
        self._session_user_agent: str = random.choice(_USER_AGENTS)
        self._lock: Optional[asyncio.Lock] = None
        # Cookies CF obtenus via FlareSolverr — persistés entre les contextes
        self._cf_cookies: List[Dict] = []

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _ensure_browser(self):
        if self._browser is None:
            from playwright.async_api import async_playwright

            # Charger les cookies CF persistés avant de démarrer Chromium
            saved_cookies, saved_ua = _load_persisted_cookies()
            if saved_cookies:
                self._cf_cookies = saved_cookies
            if saved_ua:
                self._session_user_agent = saved_ua

            self._playwright = await async_playwright().start()

            # ── playwright-stealth v2 : hooker avant launch() ─────────────
            # hook_playwright_context() patche chromium.launch() pour injecter
            # les scripts stealth dans chaque nouveau contexte automatiquement.
            # Doit être appelé AVANT launch(), sinon sans effet.
            try:
                from playwright_stealth import Stealth  # type: ignore
                Stealth().hook_playwright_context(self._playwright)
                self._stealth_available = True
                logger.info("playwright-stealth v2 — hook actif avant launch() ✓")
            except ImportError:
                self._stealth_available = False
                logger.info("playwright-stealth absent — stealth manuel seul actif (pip install playwright-stealth pour plus)")
            except Exception as e:
                self._stealth_available = False
                logger.warning(f"playwright-stealth hook échoué : {e}")

            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    # Sandboxing
                    "--no-sandbox",
                    "--disable-setuid-sandbox",

                    # ── Anti-détection headless ──────────────────────────────
                    "--disable-blink-features=AutomationControlled",

                    # Désactive les flags headless visibles
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-site-isolation-trials",

                    # Performances / stabilité container
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-zygote",

                    # Fenêtre réaliste (non-round number)
                    "--window-size=1366,768",

                    # Activer les codecs / WebGL comme un vrai browser
                    "--enable-webgl",
                    "--use-gl=swiftshader",
                    "--enable-accelerated-2d-canvas",

                    # Désactiver les extensions mais garder l'apparence normale
                    "--disable-extensions",

                    # Ignorer les erreurs de certificat (pratique pour les intranets)
                    "--ignore-certificate-errors",

                    # Eviter les infobars "Chrome est contrôlé par un test automatisé"
                    "--disable-infobars",

                    # Profil temporaire pour éviter la détection par fingerprint persistant
                    "--incognito",

                    # Activer les médias (évite des flags WebRTC)
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",

                    # Réduire les fuites de timing
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows",
                ],
            )
            logger.info("Chromium démarré (mode stealth)")

    async def _get_context(self, new_context: bool = False):
        await self._ensure_browser()
        if self._context is None or new_context:
            if self._context and not self._session_active:
                await self._context.close()

            ua = self._session_user_agent
            ch_ua = _get_ch_ua_headers(ua)

            # Headers de base — cohérents avec Chrome 136
            base_headers = {
                "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Encoding": "gzip, deflate, br, zstd",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            }
            # Ajouter Sec-CH-UA uniquement pour Chrome (pas Firefox)
            base_headers.update(ch_ua)

            self._context = await self._browser.new_context(
                user_agent=ua,
                viewport={"width": 1366, "height": 768},
                locale="fr-FR",
                timezone_id="Europe/Paris",
                extra_http_headers=base_headers,
                # Java désactivé, images activées — comme un vrai browser
                java_script_enabled=True,
                # Permissions accordées comme un vrai user
                permissions=["geolocation"],
                color_scheme="light",
                # Masquer qu'on est en headless via le deviceScaleFactor
                device_scale_factor=1.0,
                is_mobile=False,
                has_touch=False,
            )

            # ── Injecter le script stealth sur TOUTES les pages ────────────
            await self._context.add_init_script(_STEALTH_INIT_SCRIPT)

            # ── Réinjecter les cookies CF persistés (FlareSolverr) ────────
            if self._cf_cookies:
                await self._context.add_cookies(self._cf_cookies)
                logger.info(f"Cookies CF persistés réinjectés : {len(self._cf_cookies)} cookies")

        return self._context

    async def _apply_stealth_to_page(self, page):
        """Applique playwright-stealth à une page spécifique si disponible."""
        if getattr(self, "_stealth_available", False):
            try:
                from playwright_stealth import stealth_async
                await stealth_async(page, self._stealth_cfg)
            except Exception as e:
                logger.debug(f"stealth_async failed: {e}")

    async def _human_delay(self, min_ms: int = 300, max_ms: int = 1200):
        await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))

    async def _human_scroll(self, page, steps: int = 3):
        """Scroll humain avec accélération/décélération réaliste."""
        for i in range(steps):
            # Amplitude variable, plus forte au milieu
            amplitude = random.randint(150, 500) if i > 0 else random.randint(50, 200)
            await page.mouse.wheel(0, amplitude)
            await self._human_delay(100 + i * 50, 300 + i * 100)

    async def _human_mouse_move(self, page):
        """Déplace la souris de façon aléatoire pour simuler une présence humaine."""
        vp = page.viewport_size or {"width": 1366, "height": 768}
        for _ in range(random.randint(1, 3)):
            x = random.randint(100, vp["width"] - 100)
            y = random.randint(100, vp["height"] - 200)
            await page.mouse.move(x, y)
            await self._human_delay(50, 150)

    async def _extract_markdown(self, page) -> str:
        content = await page.evaluate("""() => {
            ['script','style','nav','header','footer','aside',
             '[class*="cookie"]','[class*="banner"]','[class*="popup"]',
             '[id*="cookie"]','[id*="modal"]','[class*="pub"]','[class*="ad-"]',
             '[id*="ad"]','iframe','noscript'].forEach(sel => {
                try { document.querySelectorAll(sel).forEach(el => el.remove()); } catch(e) {}
            });

            const article = document.querySelector(
                'article, [role="main"], main, .content, #content, ' +
                '.post, .article, .entry-content, .post-content, #main-content'
            );
            const body = article || document.body;

            function nodeToMd(node) {
                if (!node) return '';
                let text = '';
                for (const child of node.childNodes) {
                    if (child.nodeType === 3) {
                        const t = child.textContent.trim();
                        if (t) text += t + ' ';
                    } else if (child.nodeType === 1) {
                        const tag = child.tagName.toLowerCase();
                        const inner = nodeToMd(child);
                        if (!inner.trim()) continue;
                        if (/^h[1-6]$/.test(tag)) {
                            text += '\\n' + '#'.repeat(+tag[1]) + ' ' + inner.trim() + '\\n';
                        } else if (tag === 'p') {
                            text += '\\n' + inner.trim() + '\\n';
                        } else if (tag === 'li') {
                            text += '\\n- ' + inner.trim();
                        } else if (tag === 'a') {
                            const href = child.getAttribute('href');
                            text += (href && !href.startsWith('#'))
                                ? '[' + inner.trim() + '](' + href + ')'
                                : inner;
                        } else if (/^(strong|b)$/.test(tag)) {
                            text += '**' + inner.trim() + '**';
                        } else if (/^(em|i)$/.test(tag)) {
                            text += '*' + inner.trim() + '*';
                        } else if (tag === 'code') {
                            text += '`' + inner.trim() + '`';
                        } else if (tag === 'pre') {
                            text += '\\n```\\n' + inner.trim() + '\\n```\\n';
                        } else if (tag === 'blockquote') {
                            text += '\\n> ' + inner.trim().replace(/\\n/g,'\\n> ') + '\\n';
                        } else if (/^(div|section|article|main|figure)$/.test(tag)) {
                            text += '\\n' + inner;
                        } else {
                            text += inner;
                        }
                    }
                }
                return text;
            }
            return nodeToMd(body);
        }""")
        md = re.sub(r'\n{3,}', '\n\n', content or "")
        md = re.sub(r'[ \t]+', ' ', md)
        return md.strip()

    # ------------------------------------------------------------------
    # Recherche — stratégie multi-tentative avec fallback automatique
    # ------------------------------------------------------------------

    async def _try_search_strategy(self, page, strategy: dict, query: str, nb: int) -> List[Dict]:
        """Tente une stratégie de recherche. Retourne [] si échoue."""
        url = strategy["url"].replace("{q}", query.replace(" ", "+"))
        try:
            await self._apply_stealth_to_page(page)
            resp = await page.goto(url, wait_until=strategy["wait"], timeout=15000)
            if resp and resp.status >= 400:
                logger.debug(f"search strategy blocked: HTTP {resp.status} on {url[:60]}")
                return []

            # Vérifier challenge CF
            if await _is_cf_challenge(page):
                resolved = await _wait_cf_challenge(page, max_wait_s=10)
                if not resolved:
                    return []

            await self._human_delay(600, 1200)
            results = await page.evaluate(strategy["js"], nb)
            return [r for r in (results or []) if r and r.get("url", "").startswith("http")]
        except Exception as e:
            logger.debug(f"search strategy failed: {e}")
            return []

    async def search(
        self,
        query: str,
        engine: str = "duckduckgo",
        nb_results: int = 10,
        human_mode: bool = True,
    ) -> List[Dict[str, str]]:
        async with self._get_lock():
            ctx = await self._get_context()
            page = await ctx.new_page()
            results = []
            try:
                if human_mode:
                    await self._human_delay(200, 600)

                # ── Détection et traitement des requêtes site: ────────────
                # Les moteurs de recherche bloquent systématiquement les
                # requêtes site:domaine depuis des IPs container/datacenter.
                # Stratégie : reformuler sans site: pour les moteurs,
                # et en parallèle préparer un fallback navigation directe.
                site_domain, clean_query = _extract_site_operator(query)

                # Requête envoyée aux moteurs : sans le site: (plus de chances de passer)
                search_query = clean_query if site_domain else query

                engines_to_try = [engine] + [e for e in _ENGINE_FALLBACK if e != engine]

                for eng in engines_to_try:
                    strategies = _SEARCH_STRATEGIES.get(eng, [])
                    for strat in strategies:
                        results = await self._try_search_strategy(page, strat, search_query, nb_results)
                        if results:
                            if site_domain:
                                filtered = [r for r in results if site_domain in r.get("url", "")]
                                if len(filtered) >= max(2, nb_results // 2):
                                    logger.info(f"search({eng}, '{query}'): {len(filtered)} résultats filtrés sur {site_domain}")
                                    return filtered[:nb_results]
                                # Résultats insuffisants sur le domaine — continuer vers fallback
                                logger.info(f"search({eng}, '{query}'): {len(filtered)}/{len(results)} sur {site_domain} — fallback navigation directe")
                            else:
                                logger.info(f"search({eng}, '{query}'): {len(results)} résultats [stratégie OK]")
                                return results[:nb_results]
                    if results and not site_domain:
                        break

                # ── Fallback navigation directe pour requêtes site: ────────
                # Toujours tenté si site_domain est présent et résultats insuffisants
                if site_domain and clean_query:
                    logger.info(f"search site: navigation directe sur {site_domain}")
                    direct_results = await _search_on_site(page, site_domain, clean_query, nb_results)
                    if direct_results:
                        logger.info(f"search site: {len(direct_results)} résultats via navigation directe")
                        return direct_results[:nb_results]

                logger.warning(f"search('{query}'): tous les moteurs ont échoué")
                return []
            finally:
                await page.close()

    # ------------------------------------------------------------------
    # Navigation — wait adaptatif + stealth + gestion CF
    # ------------------------------------------------------------------

    async def navigate_and_extract(
        self,
        url: str,
        human_mode: bool = True,
        wait_for: str = "auto",
        timeout_ms: int = 30000,
        extract_format: str = "markdown",
        screenshot: bool = False,
        css_selector: Optional[str] = None,
        full_page: bool = False,
    ) -> Dict[str, Any]:
        async with self._get_lock():
            ctx = await self._get_context()
            page = await ctx.new_page()

            # Appliquer stealth sur la page elle-même
            await self._apply_stealth_to_page(page)

            # Bloquer les ressources inutiles
            await page.route(
                re.compile(r"\.(woff2?|ttf|eot|otf)(\?.*)?$", re.I),
                lambda r: r.abort()
            )
            await page.route(
                re.compile(
                    r"(googlesyndication|doubleclick|google-analytics|googletagmanager"
                    r"|facebook\.net|fbcdn|twitter\.com/i/adsct|amazon-adsystem"
                    r"|scorecardresearch|chartbeat|quantserve|outbrain|taboola)", re.I
                ),
                lambda r: r.abort()
            )

            result: Dict[str, Any] = {
                "url": url, "title": "", "content": "", "screenshot_b64": None, "links": []
            }
            try:
                if human_mode:
                    await self._human_delay(200, 600)

                # Stratégie wait adaptative
                try:
                    response = await page.goto(
                        url, wait_until="domcontentloaded", timeout=min(timeout_ms, 20000)
                    )
                    try:
                        await page.wait_for_load_state("networkidle", timeout=3000)
                    except Exception:
                        pass
                except Exception as e:
                    result["error"] = f"Navigation: {e}"
                    return result

                result["status"] = response.status if response else 0

                # ── Détection et acceptation consent wall RGPD ────────────
                consented = await _handle_consent_wall(page)
                if consented:
                    # Attendre le rechargement post-consentement
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        await asyncio.sleep(2)
                    # Second passage pour les flows en 2 étapes
                    await _handle_consent_wall(page)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass

                # ── Détection et attente challenge Cloudflare ──────────────
                if await _is_cf_challenge(page):
                    logger.info(f"Challenge CF détecté sur {url[:60]}")
                    if human_mode:
                        # Simuler une interaction humaine pendant l'attente
                        await self._human_delay(500, 1000)
                        await self._human_mouse_move(page)
                    resolved = await _wait_cf_challenge(page, max_wait_s=15)
                    if not resolved:
                        # ── Fallback FlareSolverr — headful Chromium sous Xvfb ──
                        solution = await _flaresolverr_get(url, timeout_ms=60000)
                        if solution:
                            # FlareSolverr retourne les cookies CF valides (cf_clearance, etc.)
                            # On les injecte dans le contexte Playwright puis on recharge
                            # la vraie URL — Playwright navigue alors sans déclencher CF.
                            raw_cookies = solution.get("cookies", [])
                            if raw_cookies:
                                # Normaliser vers le format Playwright
                                pw_cookies = []
                                for c in raw_cookies:
                                    pw_cookie = {
                                        "name":   c.get("name", ""),
                                        "value":  c.get("value", ""),
                                        "domain": c.get("domain", ""),
                                        "path":   c.get("path", "/"),
                                    }
                                    if c.get("secure") is not None:
                                        pw_cookie["secure"] = bool(c["secure"])
                                    if c.get("httpOnly") is not None:
                                        pw_cookie["httpOnly"] = bool(c["httpOnly"])
                                    if c.get("expiry"):
                                        pw_cookie["expires"] = float(c["expiry"])
                                    pw_cookies.append(pw_cookie)
                                await self._context.add_cookies(pw_cookies)
                                # Persister pour les prochains contextes et redémarrages
                                self._cf_cookies = pw_cookies
                                _save_persisted_cookies(pw_cookies, self._session_user_agent)
                                logger.info(f"FlareSolverr : {len(pw_cookies)} cookies CF injectés et persistés")

                            # Recharger la vraie URL avec les cookies CF actifs
                            fs_ua = solution.get("userAgent", "")
                            if fs_ua and fs_ua != self._session_user_agent:
                                # CF vérifie la cohérence UA + cookies cf_clearance —
                                # recréer le contexte avec l'UA de FlareSolverr
                                self._session_user_agent = fs_ua
                                await page.close()
                                await self._get_context(new_context=True)
                                await self._context.add_cookies(pw_cookies)
                                page = await self._context.new_page()
                                await self._apply_stealth_to_page(page)

                            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                            try:
                                await page.wait_for_load_state("networkidle", timeout=5000)
                            except Exception:
                                pass

                            result["_via_flaresolverr"] = True
                            result["status"] = response.status if response else 0
                            # Laisser la suite du flux extraire normalement ↓
                        else:
                            result["error"] = (
                                "Challenge Cloudflare non résolu après 15s. "
                                "FlareSolverr absent ou non joignable — ajoutez le service "
                                "dans docker-compose (voir README)."
                            )
                            result["title"] = await page.title()
                            return result   # erreur dure : on abandonne
                    # Recharger l'état après résolution CF
                    result["status"] = (await page.goto(page.url, wait_until="domcontentloaded", timeout=15000) or response).status

                if human_mode:
                    await self._human_delay(300, 800)
                    await self._human_scroll(page, steps=random.randint(1, 3))
                    await self._human_mouse_move(page)

                result["title"] = await page.title()
                result["url_final"] = page.url

                if extract_format == "markdown":
                    result["content"] = await self._extract_markdown(page)
                elif extract_format == "html":
                    result["content"] = await page.content()
                elif extract_format == "text":
                    result["content"] = await page.inner_text("body")
                else:
                    result["content"] = await self._extract_markdown(page)

                # ── Détection paywall avant JS-only ───────────────────────
                # Évite 15s d'attente inutile sur des sites à accès restreint
                if len(result["content"]) < 500:
                    paywall = await _detect_paywall(page)
                    if paywall:
                        result["error"] = paywall
                        result["content"] = ""
                        logger.info(f"Paywall détecté sur {url[:60]} : {paywall}")
                    else:
                        # ── Détection page JS-only ────────────────────────
                        result["content"] = await _wait_for_js_content(page, result["content"], extract_format)

                links = await page.evaluate("""() =>
                    Array.from(document.querySelectorAll('a[href]'))
                        .map(a => ({ text: a.textContent.trim().slice(0,80), href: a.href }))
                        .filter(l => l.href.startsWith('http') && l.text)
                        .slice(0, 30)
                """)
                result["links"] = links
                logger.info(f"navigate({url[:60]}): '{result['title']}' — {len(result['content'])} chars")

                if screenshot:
                    target = page.locator(css_selector) if css_selector else page
                    png_bytes = await target.screenshot(type="png", full_page=full_page)
                    compressed = _compress_screenshot(png_bytes)
                    result["screenshot_b64"] = base64.b64encode(compressed).decode()
                    result["screenshot_mime"] = "image/jpeg" if compressed[:3] == b'\xff\xd8\xff' else "image/png"

            except Exception as e:
                result["error"] = str(e)
                logger.warning(f"navigate_and_extract({url[:60]}): {e}")
            finally:
                if not self._session_active:
                    await page.close()
            return result

    async def click_element(self, page_url: str, selector: str, human_mode: bool = True) -> Dict:
        async with self._get_lock():
            ctx = await self._get_context()
            page = await ctx.new_page()
            await self._apply_stealth_to_page(page)
            result: Dict[str, Any] = {}
            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=20000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass
                if await _is_cf_challenge(page):
                    await _wait_cf_challenge(page, max_wait_s=15)
                if human_mode:
                    await self._human_delay(400, 900)
                    elem = page.locator(selector).first
                    box = await elem.bounding_box()
                    if box:
                        await page.mouse.move(
                            box["x"] + box["width"] / 2 + random.randint(-5, 5),
                            box["y"] + box["height"] / 2 + random.randint(-3, 3),
                        )
                        await self._human_delay(100, 300)
                await page.locator(selector).first.click()
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                result["url_after"] = page.url
                result["title"] = await page.title()
                result["content"] = await self._extract_markdown(page)
            except Exception as e:
                result["error"] = str(e)
            finally:
                await page.close()
            return result

    async def fill_form(
        self, page_url: str, fields: Dict[str, str],
        submit_selector: Optional[str] = None, human_mode: bool = True,
    ) -> Dict[str, Any]:
        async with self._get_lock():
            ctx = await self._get_context()
            page = await ctx.new_page()
            await self._apply_stealth_to_page(page)
            result: Dict[str, Any] = {}
            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=20000)
                if await _is_cf_challenge(page):
                    await _wait_cf_challenge(page, max_wait_s=15)
                if human_mode:
                    await self._human_delay(500, 1200)
                for selector, value in fields.items():
                    await page.locator(selector).first.click()
                    if human_mode:
                        await self._human_delay(100, 300)
                        await page.locator(selector).first.type(value, delay=random.randint(40, 120))
                    else:
                        await page.locator(selector).first.fill(value)
                    if human_mode:
                        await self._human_delay(200, 500)
                if submit_selector:
                    if human_mode:
                        await self._human_delay(400, 900)
                    await page.locator(submit_selector).first.click()
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                result["url_after"] = page.url
                result["title"] = await page.title()
                result["content"] = await self._extract_markdown(page)
            except Exception as e:
                result["error"] = str(e)
            finally:
                await page.close()
            return result

    async def close(self):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()


_client: Optional[_BrowserClient] = None

def _get_client() -> _BrowserClient:
    global _client
    if _client is None:
        _client = _BrowserClient()
    return _client


# ---------------------------------------------------------------------------
# Helpers formatage
# ---------------------------------------------------------------------------

def _fmt_search_result(r: Dict, idx: int) -> str:
    lines = [f"{idx}. **{r.get('title', 'Sans titre')}**"]
    if r.get("url"):
        lines.append(f"   {r['url']}")
    if r.get("snippet"):
        lines.append(f"   {r['snippet'][:300]}{'…' if len(r['snippet'])>300 else ''}")
    return "\n".join(lines)


def _fmt_page_result(r: Dict, max_content: int = 8000) -> str:
    lines = []
    if r.get("title"):
        lines.append(f"# {r['title']}")
    url_final = r.get("url_final", r.get("url", ""))
    if url_final:
        lines.append(f"**URL** : {url_final}  (HTTP {r.get('status', '?')})")
    lines.append("")
    if r.get("error"):
        lines.append(f"⚠️ **Erreur** : {r['error']}")
    elif r.get("content"):
        lines.append(r["content"][:max_content])
        if len(r["content"]) > max_content:
            lines.append(f"\n*[Contenu tronqué — {len(r['content'])} chars au total]*")
    links = r.get("links", [])
    if links:
        lines.append(f"\n**Liens ({len(links)})** :")
        for lnk in links[:10]:
            lines.append(f"- [{lnk.get('text','')}]({lnk.get('href','')})")
    return "\n".join(lines)


# ===========================================================================
# OUTILS MCP
# ===========================================================================

@mcp.tool()
async def browser_rechercher(
    query: str,
    moteur: str = "duckduckgo",
    nb_resultats: int = 10,
    mode_humain: bool = True,
) -> str:
    """Effectue une recherche web via un vrai navigateur Chromium headless.

    Tente plusieurs stratégies par moteur et bascule automatiquement sur
    un moteur de repli si le moteur demandé est bloqué.
    Retourne une liste de résultats avec titre, URL et extrait.

    Args:
        query: Requête de recherche (ex: 'actualité du jour Le Monde').
        moteur: Moteur préféré : 'duckduckgo' (défaut), 'bing', 'google', 'searxng'.
        nb_resultats: Nombre de résultats (max 20, défaut 10).
        mode_humain: Si true, simule des délais humains (défaut: true).
    """
    c = _get_client()
    results = await c.search(query, engine=moteur, nb_results=min(nb_resultats, 20), human_mode=mode_humain)
    if not results:
        return f"Aucun résultat trouvé pour : « {query} » — tous les moteurs ont été essayés."
    lines = [f"**{len(results)} résultat(s) pour « {query} »**\n"]
    for i, r in enumerate(results, 1):
        lines.append(_fmt_search_result(r, i))
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
async def browser_naviguer(
    url: str,
    format_extraction: str = "markdown",
    screenshot: bool = False,
    mode_humain: bool = True,
    attente: str = "auto",
    timeout_ms: int = 25000,
) -> str:
    """Navigue vers une URL et extrait son contenu.

    Supporte les pages JavaScript complexes, shadow DOM, React.
    Utilise une stratégie d'attente adaptative (domcontentloaded + 3s networkidle)
    pour éviter les timeouts sur les sites lourds.
    Gère automatiquement les challenges Cloudflare (attente résolution JS).
    Bloque automatiquement pub et analytics pour accélérer le chargement.

    Args:
        url: URL complète à charger (ex: 'https://www.legifrance.gouv.fr').
        format_extraction: 'markdown' (défaut), 'html', 'text'.
        screenshot: Si true, retourne un screenshot base64 (défaut: false).
        mode_humain: Si true, simule scroll et délais humains (défaut: true).
        attente: 'auto' (défaut — adaptatif), 'domcontentloaded', 'load', 'networkidle'.
        timeout_ms: Timeout en ms (défaut: 25000).
    """
    c = _get_client()
    result = await c.navigate_and_extract(
        url, human_mode=mode_humain, wait_for=attente,
        timeout_ms=timeout_ms, extract_format=format_extraction, screenshot=screenshot,
    )
    output = _fmt_page_result(result)
    if result.get("screenshot_b64"):
        b64 = result["screenshot_b64"]
        mime = result.get("screenshot_mime", "image/png")
        meta = {"mime": mime, "alt": f"Screenshot de {result.get('title', url)}"}
        import json as _json
        output += f"\n\n```screenshot\n{_json.dumps(meta)}\n{b64}\n```"
    return output


@mcp.tool()
async def browser_screenshot(
    url: str,
    selecteur_css: Optional[str] = None,
    pleine_page: bool = False,
) -> list[ImageContent | TextContent]:
    """Prend un screenshot d'une page web ou d'un élément spécifique.

    Retourne un bloc image MCP natif (type=image) pour affichage direct dans le chat.

    Args:
        url: URL de la page à capturer.
        selecteur_css: Sélecteur CSS d'un élément spécifique (ex: '#main-chart').
                       Si absent, capture le viewport visible.
        pleine_page: Si true, capture la page entière avec défilement. Défaut: false.
    """
    c = _get_client()
    result = await c.navigate_and_extract(
        url, screenshot=True, css_selector=selecteur_css,
        extract_format="markdown", human_mode=True, full_page=pleine_page,
    )
    if result.get("error"):
        return [TextContent(type="text", text=f"⚠️ Erreur lors de la capture : {result['error']}")]
    b64 = result.get("screenshot_b64", "")
    mime = result.get("screenshot_mime", "image/jpeg")
    if not b64:
        return [TextContent(type="text", text=f"⚠️ Screenshot vide pour : {url}")]
    title = result.get('title', url)
    url_final = result.get('url_final', url)
    size_kb = len(b64) * 3 // 4 // 1024

    return [
        TextContent(type="text", text=f"Screenshot de {title} ({url_final}, {size_kb} Ko) — image transmise et affichée directement dans le chat par le système. Ne pas la redécrire ni la mentionner comme chargement."),
        ImageContent(type="image", data=b64, mimeType=mime),
    ]


@mcp.tool()
async def browser_cliquer(
    url: str,
    selecteur_css: str,
    mode_humain: bool = True,
) -> str:
    """Clique sur un élément d'une page et retourne le contenu résultant.

    Utile pour interagir avec des boutons, onglets, accordéons, pagination, etc.
    Attend la fin du chargement JS après le clic.

    Args:
        url: URL de la page.
        selecteur_css: Sélecteur CSS de l'élément à cliquer (ex: 'button.load-more').
        mode_humain: Si true, déplace la souris naturellement avant de cliquer (défaut: true).
    """
    c = _get_client()
    result = await c.click_element(url, selecteur_css, human_mode=mode_humain)
    if result.get("error"):
        return f"⚠️ Erreur lors du clic sur `{selecteur_css}` : {result['error']}"
    return (
        f"**Clic sur `{selecteur_css}` effectué**\n"
        f"URL résultante : {result.get('url_after', '')}\n"
        f"Titre : {result.get('title', '')}\n\n"
        + result.get("content", "")[:6000]
    )


@mcp.tool()
async def browser_formulaire(
    url: str,
    champs: Dict[str, str],
    selecteur_soumission: Optional[str] = None,
    mode_humain: bool = True,
) -> str:
    """Remplit et soumet un formulaire web.

    En mode humain, tape caractère par caractère avec des délais aléatoires.

    Args:
        url: URL de la page contenant le formulaire.
        champs: Dictionnaire {sélecteur_css: valeur} des champs à remplir.
                Ex: {'#email': 'user@example.com', 'input[name=password]': 'secret'}.
        selecteur_soumission: Sélecteur CSS du bouton de soumission.
                              Si absent, les champs sont remplis sans soumettre.
        mode_humain: Si true, simule une frappe humaine (défaut: true).
    """
    c = _get_client()
    result = await c.fill_form(url, champs, selecteur_soumission, human_mode=mode_humain)
    if result.get("error"):
        return f"⚠️ Erreur lors du remplissage : {result['error']}"
    status = "soumis" if selecteur_soumission else "rempli (non soumis)"
    return (
        f"**Formulaire {status}**\n"
        f"URL résultante : {result.get('url_after', url)}\n"
        f"Titre : {result.get('title', '')}\n\n"
        + result.get("content", "")[:6000]
    )


@mcp.tool()
async def browser_research(
    query: str,
    nb_sources: int = 5,
    moteur: str = "duckduckgo",
    longueur_max_par_source: int = 3000,
) -> str:
    """Mode recherche approfondie : ouvre plusieurs sources et retourne les extraits consolidés.

    Effectue une recherche, visite les N premières URLs, extrait le contenu
    de chacune. Idéal pour comparer des informations ou construire une réponse documentée.

    Args:
        query: Sujet de la recherche.
        nb_sources: Nombre de sources à visiter (max 8, défaut 5).
        moteur: Moteur préféré : 'duckduckgo' (défaut), 'bing', 'google', 'searxng'.
        longueur_max_par_source: Caractères max par source (défaut: 3000).
    """
    c = _get_client()
    nb_sources = min(nb_sources, 8)

    search_results = await c.search(query, engine=moteur, nb_results=nb_sources + 3, human_mode=True)
    if not search_results:
        return f"Aucun résultat de recherche pour : « {query} »"

    lines = [f"# Recherche approfondie : « {query} »\n"]
    lines.append(f"**{len(search_results)} sources trouvées — visite des {nb_sources} premières**\n---\n")

    sources_content, visited = [], 0
    for sr in search_results:
        if visited >= nb_sources:
            break
        url = sr.get("url", "")
        if not url or not url.startswith("http"):
            continue
        try:
            page_result = await c.navigate_and_extract(
                url, human_mode=True, wait_for="auto", timeout_ms=20000
            )
            content = page_result.get("content", "")[:longueur_max_par_source]
            title   = page_result.get("title", sr.get("title", url))
            if content and not page_result.get("error"):
                sources_content.append({"title": title, "url": url, "content": content})
                visited += 1
        except Exception as e:
            logger.warning(f"Research: erreur visite {url[:60]}: {e}")

    if not sources_content:
        return f"Impossible de visiter les sources pour : « {query} »"

    for i, src in enumerate(sources_content, 1):
        lines.append(f"## Source {i} : {src['title']}")
        lines.append(f"*{src['url']}*\n")
        lines.append(src["content"])
        lines.append("\n---\n")

    lines.append(f"*{len(sources_content)} sources visitées.*")
    return "\n".join(lines)


@mcp.tool()
async def browser_session_demarrer(url_initiale: Optional[str] = None) -> str:
    """Démarre une session de navigation persistante (cookies, logins conservés).

    La session conserve l'état du navigateur entre les appels :
    cookies, sessions authentifiées, localStorage.
    Utile pour rester connecté à un service ou pour contourner des protections
    anti-bot qui persistent après le premier challenge résolu.

    Args:
        url_initiale: URL à charger au démarrage de la session (optionnel).
    """
    c = _get_client()
    c._session_active = True
    ctx = await c._get_context(new_context=True)

    title = None
    if url_initiale:
        page = await ctx.new_page()
        await c._apply_stealth_to_page(page)
        await page.goto(url_initiale, wait_until="domcontentloaded", timeout=25000)
        if await _is_cf_challenge(page):
            await _wait_cf_challenge(page, max_wait_s=15)
        title = await page.title()
        await page.close()

    msg = "**Session persistante démarrée** — cookies et logins conservés entre les appels."
    if title:
        msg += f"\nPage initiale chargée : **{title}**"
    msg += "\n\nUtilisez `browser_naviguer`, `browser_cliquer` ou `browser_formulaire` normalement."
    msg += "\nArrêtez avec `browser_session_arreter` pour libérer les ressources."
    return msg


@mcp.tool()
async def browser_session_arreter() -> str:
    """Arrête la session persistante et libère les ressources navigateur."""
    c = _get_client()
    c._session_active = False
    if c._context:
        await c._context.close()
        c._context = None
    return "**Session persistante arrêtée.** Contexte, cookies et état effacés."


@mcp.tool()
async def browser_healthcheck() -> str:
    """Vérifie que le navigateur Playwright/Chromium est opérationnel.

    Lance Chromium, charge une page de test et retourne le statut.
    Indique également si playwright-stealth est disponible.
    """
    c = _get_client()
    await c._ensure_browser()
    ctx = await c._get_context()
    page = await ctx.new_page()
    await c._apply_stealth_to_page(page)
    stealth_status = "playwright-stealth ✅" if getattr(c, "_stealth_available", False) else "stealth manuel seul ⚠️ (pip install playwright-stealth)"
    try:
        resp = await page.goto(
            "https://html.duckduckgo.com/html/?q=test",
            wait_until="domcontentloaded", timeout=15000
        )
        title = await page.title()
        status = resp.status if resp else 0
        ok = "✅" if status < 400 else "⚠️"
        return (
            f"**Browser MCP healthcheck** : {ok} Chromium opérationnel — '{title}' (HTTP {status})\n"
            f"**Stealth** : {stealth_status}\n"
            f"**User-Agent** : {c._session_user_agent}"
        )
    except Exception as e:
        return f"**Browser MCP healthcheck** : ⚠️ Erreur : {e}"
    finally:
        await page.close()


# ---------------------------------------------------------------------------
# Helpers recherche d'images multi-sources
# ---------------------------------------------------------------------------

async def _http_get_json(url: str) -> Optional[Dict]:
    """
    Requête HTTP GET JSON — essaie aiohttp, puis urllib avec headers navigateur.
    Utilisé par les helpers d'images pour éviter les blocages 403.
    """
    import urllib.parse, urllib.request, json

    # 1. aiohttp si disponible (headers navigateur complets)
    try:
        import aiohttp
        async with aiohttp.ClientSession(headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
            "Accept": "application/json",
        }) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
        return None
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"_http_get_json aiohttp: {e}")

    # 2. urllib avec headers navigateur
    import asyncio
    def _fetch():
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
            "Accept": "application/json, */*",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _fetch)
    except Exception as e:
        logger.debug(f"_http_get_json urllib: {e}")
        return None


async def _search_wikimedia_images(query: str, nb: int) -> List[Dict]:
    """Wikimedia Commons API — sans auth, fiable depuis container."""
    import urllib.parse
    try:
        q = urllib.parse.quote_plus(query)
        # Utiliser generator=search + prop=imageinfo pour obtenir les URLs directement
        # sans avoir à reconstruire le chemin MD5 (source d'erreurs)
        api_url = (
            f"https://commons.wikimedia.org/w/api.php"
            f"?action=query&generator=search&gsrsearch={q}+filemime%3Aimage&gsrnamespace=6"
            f"&gsrlimit={nb * 3}&prop=imageinfo&iiprop=url|mime|extmetadata"
            f"&iiurlwidth=800&format=json&origin=*"
        )
        data = await _http_get_json(api_url)
        if not data:
            return []

        results = []
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            if len(results) >= nb:
                break
            title = page.get("title", "")
            imageinfo = page.get("imageinfo", [])
            if not imageinfo:
                continue
            info = imageinfo[0]
            mime = info.get("mime", "")
            # Ne garder que les images affichables
            if not mime.startswith("image/") or mime in ("image/tiff", "image/x-xcf"):
                continue
            # thumburl = URL du thumbnail 800px, url = URL originale
            img_url = info.get("thumburl") or info.get("url", "")
            if not img_url or not img_url.startswith("http"):
                continue
            page_url = f"https://commons.wikimedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
            # Licence depuis extmetadata
            meta = info.get("extmetadata", {})
            license_name = (meta.get("LicenseShortName") or {}).get("value", "Wikimedia Commons")
            fname = title[5:] if title.startswith("File:") else title
            results.append({
                "title": fname,
                "url": img_url,
                "source": page_url,
                "license": license_name,
            })

        return results
    except Exception as e:
        logger.warning(f"_search_wikimedia_images: {e}")
        return []


async def _search_openverse_images(query: str, nb: int) -> List[Dict]:
    """OpenVerse API — catalogue Creative Commons, sans auth pour usage basique."""
    import urllib.parse
    try:
        q = urllib.parse.quote_plus(query)
        url = f"https://api.openverse.org/v1/images/?q={q}&page_size={nb}&license_type=commercial,modification"
        data = await _http_get_json(url)
        if not data:
            return []
        results = []
        for item in data.get("results", [])[:nb]:
            img_url = item.get("url", "")
            if not img_url:
                continue
            results.append({
                "title": item.get("title") or item.get("creator") or query,
                "url": img_url,
                "source": item.get("foreign_landing_url", ""),
                "license": item.get("license_url", item.get("license", "")),
            })
        return results
    except Exception as e:
        logger.warning(f"_search_openverse_images: {e}")
        return []


async def _search_bing_images(query: str, nb: int) -> List[Dict]:
    """Bing Images — fallback headless, résultats limités depuis container."""
    import urllib.parse
    c = _get_client()
    async with c._get_lock():
        ctx = await c._get_context()
        page = await ctx.new_page()
        await c._apply_stealth_to_page(page)
        try:
            q = urllib.parse.quote_plus(query)
            await page.goto(
                f"https://www.bing.com/images/search?q={q}&setlang=fr&count=20",
                wait_until="domcontentloaded", timeout=20000
            )
            await _handle_consent_wall(page)
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass

            images = await page.evaluate("""(nb) => {
                const results = [];
                document.querySelectorAll('a.iusc, .imgpt a').forEach(a => {
                    if (results.length >= nb) return;
                    try {
                        const m = a.getAttribute('m') || a.getAttribute('data-m') || '';
                        if (m) {
                            const meta = JSON.parse(m);
                            const imgUrl = meta.murl || meta.imgurl || '';
                            if (imgUrl && imgUrl.startsWith('http')) {
                                results.push({
                                    title: meta.t || meta.desc || '',
                                    url: imgUrl,
                                    source: meta.purl || '',
                                    license: ''
                                });
                            }
                        }
                    } catch(e) {}
                });
                return results.slice(0, nb);
            }""", nb)
            return images or []
        except Exception as e:
            logger.debug(f"_search_bing_images: {e}")
            return []
        finally:
            await page.close()



@mcp.tool()
async def browser_chercher_images(
    requete: str,
    nb_resultats: int = 5,
    source: str = "auto",
) -> str:
    """Recherche des images sur le web et retourne leurs URLs directes.

    Stratégie multi-sources ordonnée par fiabilité :
    1. Wikimedia Commons (API REST publique, sans auth, très fiable)
    2. OpenVerse — catalogue Creative Commons
    3. Bing Images (fallback headless)

    Args:
        requete: Description de l'image cherchée (ex: 'Amiga 500', 'Sara Giraudeau').
        nb_resultats: Nombre d'images à retourner (max 10, défaut 5).
        source: 'auto' (défaut), 'wikimedia', 'openverse', 'bing'.
    """
    import urllib.parse
    nb = min(nb_resultats, 10)

    images: List[Dict] = []

    # ── 1. Wikimedia Commons API ───────────────────────────────────────────
    if source in ("auto", "wikimedia"):
        images = await _search_wikimedia_images(requete, nb)
        if images:
            logger.info(f"browser_chercher_images('{requete}'): {len(images)} images Wikimedia")

    # ── 2. OpenVerse API ──────────────────────────────────────────────────
    if not images and source in ("auto", "openverse"):
        images = await _search_openverse_images(requete, nb)
        if images:
            logger.info(f"browser_chercher_images('{requete}'): {len(images)} images OpenVerse")

    # ── 3. Bing Images (headless fallback) ────────────────────────────────
    if not images and source in ("auto", "bing"):
        images = await _search_bing_images(requete, nb)
        if images:
            logger.info(f"browser_chercher_images('{requete}'): {len(images)} images Bing")

    if not images:
        return f"Aucune image trouvée pour : « {requete} »"

    lines = [f"**{len(images)} image(s) trouvée(s) pour « {requete} »**\n"]
    for i, img in enumerate(images, 1):
        lines.append(f"{i}. **{img.get('title', 'Sans titre')}**")
        lines.append(f"   Image : {img['url']}")
        if img.get('source'):
            lines.append(f"   Source : {img['source']}")
        if img.get('license'):
            lines.append(f"   Licence : {img['license']}")
        lines.append("")
    return "\n".join(lines)


# ===========================================================================
# Point d'entrée
# ===========================================================================

if __name__ == "__main__":
    import argparse
    from contextlib import asynccontextmanager

    parser = argparse.ArgumentParser(description="Serveur MCP HTTP Streamable — Browser / Playwright")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=6503)
    parser.add_argument("--path", default="/mcp")
    args = parser.parse_args()

    mcp.settings.host                 = args.host
    mcp.settings.port                 = args.port
    mcp.settings.streamable_http_path = args.path
    # stateless_http=True : compatibilité maximale avec les clients MCP (Demeter, etc.)
    # Le singleton _client est au niveau module — il survit entre les requêtes dans le
    # même process uvicorn. On pré-chauffe Chromium au démarrage via lifespan pour que
    # le premier appel ne subisse pas de cold-start détectable par les WAF.
    if hasattr(mcp.settings, "stateless_http"):
        mcp.settings.stateless_http = True

    @asynccontextmanager
    async def lifespan(app):
        """Pré-chauffe Chromium au démarrage — évite le cold-start sur la 1ère requête."""
        client = _get_client()
        try:
            await client._ensure_browser()
            ctx = await client._get_context()
            logger.info("✅ Chromium pré-chauffé et prêt (cold-start évité)")
        except Exception as e:
            logger.warning(f"⚠️  Pré-chauffe Chromium échouée : {e} — sera relancé à la 1ère requête")
        yield
        # Nettoyage propre à l'arrêt
        if _client is not None:
            await _client.close()

    # Injecter le lifespan si FastMCP le supporte
    try:
        mcp.settings.lifespan = lifespan
    except Exception:
        # Version ancienne sans support lifespan — pas critique, cold-start possible
        pass

    logger.info(f"🚀 Démarrage MCP Browser sur http://{args.host}:{args.port}{args.path}")
    logger.info("   10 outils — spec MCP 2025-03-26 HTTP Streamable")
    logger.info("   Stealth : script manuel + playwright-stealth (si installé)")
    logger.info("   Anti-CF : détection challenge + attente résolution automatique")
    logger.info("   Moteurs : DuckDuckGo · Bing · Google · SearXNG (fallback auto)")

    mcp.run(transport="streamable-http")
