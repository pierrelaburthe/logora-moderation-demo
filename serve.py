#!/usr/bin/env python3
"""
Logora — Serveur local de démo pour la modération en temps réel.

Sert moderation_lequipe_demo.html et relaie les appels vers le moteur IA Logora.
La clé du moteur et le modèle utilisé restent côté serveur, jamais exposés au navigateur.
Tous les messages d'erreur retournés au navigateur sont anonymisés (aucune
référence au fournisseur sous-jacent).

Utilisation :
    python3 serve.py
puis ouvre http://localhost:8000 dans ton navigateur.
"""

import html as html_module
import http.server
import json
import re
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

# ------------------------------------------------------------------
# Configuration interne (NE PAS exposer au navigateur)
# Priorité de lecture :
#   1. Variable d'environnement ANTHROPIC_API_KEY (pour le déploiement)
#   2. Fichier config.py local (pour le dev) — gitignored
# ------------------------------------------------------------------
import os as _os

PORT = int(_os.environ.get("PORT", "8000"))
_UPSTREAM_URL = "https://api.anthropic.com/v1/messages"

_INTERNAL_KEY = _os.environ.get("ANTHROPIC_API_KEY", "").strip()
_INTERNAL_MODEL = _os.environ.get("LOGORA_MODEL", "claude-sonnet-4-6").strip()

if not _INTERNAL_KEY:
    try:
        import config as _config  # type: ignore
        _INTERNAL_KEY = getattr(_config, "ANTHROPIC_API_KEY", "").strip()
        _INTERNAL_MODEL = getattr(_config, "LOGORA_MODEL", _INTERNAL_MODEL).strip()
    except ImportError:
        pass

if not _INTERNAL_KEY:
    print("\n⚠ Aucune clé API trouvée.")
    print("  Définis ANTHROPIC_API_KEY dans tes variables d'environnement,")
    print("  ou crée un fichier config.py contenant : ANTHROPIC_API_KEY = \"sk-ant-...\"\n")

HTML_FILE = Path(__file__).parent / "moderation_lequipe_demo.html"
CORRECTIONS_FILE = Path(__file__).parent / "corrections.json"
_CORRECTIONS_LOCK = threading.Lock()
_MAX_CORRECTIONS_IN_PROMPT = 30  # garde les 30 plus récentes pour éviter d'exploser le contexte

# Patterns à effacer dans les réponses d'erreur pour le branding Logora
_BRAND_REPLACEMENTS = [
    (re.compile(r"claude-[a-z0-9\-]+", re.I), "moteur Logora"),
    (re.compile(r"\banthropic\b", re.I), "Logora"),
    (re.compile(r"\borganization\b", re.I), "compte"),
    (re.compile(r"org:\s*[0-9a-f\-]+", re.I), "compte: ***"),
    (re.compile(r"\bmodel:\s*[a-z0-9\-]+", re.I), "moteur: Logora"),
]


def _sanitize(body_bytes: bytes) -> bytes:
    try:
        text = body_bytes.decode("utf-8", errors="replace")
    except Exception:
        return body_bytes
    for pattern, repl in _BRAND_REPLACEMENTS:
        text = pattern.sub(repl, text)
    return text.encode("utf-8")


# ------------------------------------------------------------------
# Corrections / feedback de la rédaction
# ------------------------------------------------------------------
def _load_corrections():
    with _CORRECTIONS_LOCK:
        if not CORRECTIONS_FILE.exists():
            return []
        try:
            data = json.loads(CORRECTIONS_FILE.read_text("utf-8"))
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and isinstance(data.get("corrections"), list):
                return data["corrections"]
            return []
        except Exception:
            return []


def _save_corrections(corrections):
    with _CORRECTIONS_LOCK:
        CORRECTIONS_FILE.write_text(
            json.dumps(corrections, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _add_correction(entry: dict) -> dict:
    """Ajoute une correction et retourne l'entrée enrichie."""
    entry["id"] = uuid.uuid4().hex[:12]
    entry["timestamp"] = datetime.utcnow().isoformat() + "Z"
    items = _load_corrections()
    items.append(entry)
    _save_corrections(items)
    return entry


def _delete_correction(correction_id: str) -> bool:
    items = _load_corrections()
    new = [c for c in items if c.get("id") != correction_id]
    if len(new) == len(items):
        return False
    _save_corrections(new)
    return True


def _build_corrections_block(corrections) -> str:
    """Convertit les corrections en bloc texte à appender au system prompt."""
    if not corrections:
        return ""
    # Garde les N plus récentes
    recent = corrections[-_MAX_CORRECTIONS_IN_PROMPT:]
    n = len(recent)
    lines = [
        "",
        "",
        "# AJUSTEMENTS APPORTÉS PAR LA RÉDACTION (in-context learning)",
        f"La rédaction de L'Équipe a corrigé manuellement {n} décision(s) antérieure(s).",
        "Ces corrections ont **priorité absolue** sur la charte ci-dessus en cas de doute :",
        "tu dois aligner ton jugement futur sur ces attentes.",
        "",
    ]
    for i, c in enumerate(recent, 1):
        comment = (c.get("comment") or "").strip()[:400]
        ctx_title = (c.get("article_title") or "").strip()[:200]
        ctx_topic = (c.get("article_topic") or "").strip()
        ai_decision = c.get("ai_decision") or "?"
        ai_raison = c.get("ai_raison") or "—"
        ai_score = c.get("ai_score")
        expected_decision = c.get("expected_decision") or "?"
        expected_raison = c.get("expected_raison") or "—"
        justification = (c.get("justification") or "").strip()[:600]

        lines.append(f"## Ajustement #{i}")
        lines.append(f"Commentaire : « {comment} »")
        ctx_bits = []
        if ctx_title:
            ctx_bits.append(f"article = {ctx_title}")
        if ctx_topic:
            ctx_bits.append(f"rubrique = {ctx_topic}")
        if ctx_bits:
            lines.append("Contexte : " + ", ".join(ctx_bits))
        lines.append(
            f"Décision IA initiale : {ai_decision} "
            f"(raison : {ai_raison}, score : {ai_score if ai_score is not None else '?'})"
        )
        lines.append(
            f"Décision attendue par la rédaction : **{expected_decision}**"
            + (f" (raison : {expected_raison})" if expected_raison and expected_raison != "—" else "")
        )
        if justification:
            lines.append(f"Justification : {justification}")
        lines.append("")
    lines.append(
        "→ Quand un nouveau commentaire ressemble à l'un des cas ci-dessus, "
        "applique la décision attendue par la rédaction, et calibre ton score en conséquence."
    )
    return "\n".join(lines)


# ------------------------------------------------------------------
# Récupération du contexte article depuis une URL (best effort)
# ------------------------------------------------------------------
_URL_RE = re.compile(r"^https?://", re.I)

def _looks_like_url(s: str) -> bool:
    return bool(s and _URL_RE.match(s.strip()))


def _extract_meta(page: str, names) -> str:
    """Extrait la valeur du premier <meta property|name="..."> trouvé."""
    for name in names:
        for pattern in (
            rf'<meta\s+(?:property|name)=["\']{re.escape(name)}["\']\s+content=["\']([^"\']+)["\']',
            rf'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']{re.escape(name)}["\']',
        ):
            m = re.search(pattern, page, re.I | re.S)
            if m:
                return html_module.unescape(m.group(1)).strip()
    return ""


def _extract_title_tag(page: str) -> str:
    m = re.search(r"<title[^>]*>([^<]+)</title>", page, re.I | re.S)
    if m:
        return html_module.unescape(m.group(1)).strip()
    return ""


def _fetch_article_context(url: str) -> dict:
    """Best effort : récupère titre + description courte d'une page web."""
    out = {"url": url, "title": "", "description": "", "ok": False}
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; LogoraModerationBot/1.0; +https://logora.fr)",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "html" not in content_type.lower():
                return out
            raw = resp.read(500_000)  # 500 KB max
            charset = resp.headers.get_content_charset() or "utf-8"
        page = raw.decode(charset, errors="replace")
        title = _extract_meta(page, ["og:title", "twitter:title"]) or _extract_title_tag(page)
        description = _extract_meta(page, ["og:description", "twitter:description", "description"])
        out["title"] = title[:300]
        out["description"] = description[:600]
        out["ok"] = bool(title or description)
    except Exception as e:
        out["error"] = str(e)
    return out


def _build_article_block(article_title: str, article_topic: str) -> tuple:
    """
    Construit le bloc de contexte article qui sera injecté dans le message
    utilisateur. Si article_title est une URL, on tente de récupérer
    titre + description côté serveur.

    Retourne (texte_pour_le_moteur, meta_pour_le_navigateur).
    """
    if _looks_like_url(article_title):
        url = article_title.strip()
        meta = _fetch_article_context(url)
        # Indice supplémentaire : le pathname de l'URL est souvent très
        # informatif (ex : /football/coupe-d-afrique/france-senegal-...).
        parsed = urllib.parse.urlparse(url)
        path_clean = parsed.path.replace("-", " ").replace("/", " · ").strip(" ·")
        lines = ["URL : " + url]
        if meta.get("title"):
            lines.append(f"Titre détecté : {meta['title']}")
        if meta.get("description"):
            lines.append(f"Chapeau / description : {meta['description']}")
        if path_clean and not meta.get("title"):
            lines.append(f"Indices URL : {path_clean}")
        if article_topic:
            lines.append(f"Rubrique : {article_topic}")
        return "\n".join(lines), meta
    # Sinon : titre texte libre
    lines = []
    if article_title.strip():
        lines.append(f"Titre : {article_title.strip()}")
    if article_topic:
        lines.append(f"Rubrique : {article_topic}")
    if not lines:
        lines.append("Titre : (non précisé)")
        lines.append(f"Rubrique : {article_topic or '(non précisé)'}")
    return "\n".join(lines), None


def _friendly_error(status: int, raw_body: bytes) -> bytes:
    """Reformule certaines erreurs connues pour ne rien laisser fuir."""
    if status == 429:
        return json.dumps({
            "error": {
                "type": "rate_limit",
                "message": "Cadence maximale atteinte sur ce compte de démo (5 modérations / minute). Réessai automatique.",
            }
        }).encode("utf-8")
    if status == 401:
        return json.dumps({
            "error": {
                "type": "auth",
                "message": "Clé de service Logora invalide ou révoquée.",
            }
        }).encode("utf-8")
    if status == 529 or status == 503:
        return json.dumps({
            "error": {
                "type": "overloaded",
                "message": "Moteur Logora momentanément surchargé. Réessai automatique.",
            }
        }).encode("utf-8")
    # Pour le reste, on sanitise au moins le contenu
    return _sanitize(raw_body)


class LogoraHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[Logora] {fmt % args}")

    # ------------------------------------------------------------------
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                content = HTML_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_error(404, "moderation_lequipe_demo.html introuvable")
        elif self.path == "/health":
            self._send_json(200, {"ok": True, "service": "Logora moderation"})
        elif self.path == "/api/feedback":
            items = _load_corrections()
            self._send_json(200, {"corrections": items, "count": len(items)})
        else:
            self.send_error(404)

    # ------------------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ------------------------------------------------------------------
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)

        # --- Feedback endpoints ----------------------------------------
        if self.path == "/api/feedback":
            try:
                payload = json.loads(raw_body or b"{}")
            except Exception:
                self._send_json(400, {"error": {"message": "JSON invalide."}})
                return
            for required in ("comment", "expected_decision"):
                if not payload.get(required):
                    self._send_json(400, {"error": {"message": f"Champ manquant : {required}"}})
                    return
            entry = {
                "comment": payload.get("comment", ""),
                "article_title": payload.get("article_title", ""),
                "article_topic": payload.get("article_topic", ""),
                "ai_decision": payload.get("ai_decision", ""),
                "ai_raison": payload.get("ai_raison", ""),
                "ai_score": payload.get("ai_score"),
                "expected_decision": payload.get("expected_decision", ""),
                "expected_raison": payload.get("expected_raison", ""),
                "justification": payload.get("justification", ""),
            }
            saved = _add_correction(entry)
            items = _load_corrections()
            print(f"[Logora] Nouvel ajustement rédac : {saved['id']} ({len(items)} au total)")
            self._send_json(200, {"ok": True, "correction": saved, "count": len(items)})
            return

        if self.path == "/api/feedback/delete":
            try:
                payload = json.loads(raw_body or b"{}")
            except Exception:
                self._send_json(400, {"error": {"message": "JSON invalide."}})
                return
            cid = payload.get("id")
            if not cid:
                self._send_json(400, {"error": {"message": "id manquant"}})
                return
            ok = _delete_correction(cid)
            items = _load_corrections()
            self._send_json(200, {"ok": ok, "count": len(items)})
            return

        if self.path != "/api/moderate":
            self.send_error(404)
            return

        # On reconstruit le payload côté serveur pour ne PAS exposer
        # le nom du modèle ni la structure exacte côté navigateur.
        try:
            payload_in = json.loads(raw_body or b"{}")
        except Exception:
            self._send_json(400, {"error": {"type": "bad_request", "message": "Requête invalide."}})
            return

        system_prompt = payload_in.get("system_prompt") or ""
        comment = payload_in.get("comment") or ""
        article_title = payload_in.get("article_title") or ""
        article_topic = payload_in.get("article_topic") or ""

        if not comment.strip():
            self._send_json(400, {"error": {"type": "bad_request", "message": "Commentaire manquant."}})
            return

        article_block, article_meta = _build_article_block(article_title, article_topic)
        user_message = (
            f"# CONTEXTE ARTICLE\n{article_block}\n\n"
            f"# COMMENTAIRE À MODÉRER\n{comment}"
        )
        if article_meta and article_meta.get("ok"):
            print(f"[Logora] Article détecté : {article_meta.get('title', '')[:80]}")

        # Injection des corrections de la rédaction dans le system prompt
        corrections = _load_corrections()
        if corrections:
            system_prompt = system_prompt + _build_corrections_block(corrections)
            print(f"[Logora] {len(corrections)} ajustement(s) rédac injecté(s) dans le prompt")

        upstream_payload = {
            "model": _INTERNAL_MODEL,
            "max_tokens": 400,
            "system": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user_message}],
        }

        req = urllib.request.Request(
            _UPSTREAM_URL,
            data=json.dumps(upstream_payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": _INTERNAL_KEY,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                response_body = _sanitize(resp.read())
                status_code = resp.status
                content_type = "application/json"
        except urllib.error.HTTPError as e:
            raw = e.read()
            status_code = e.code
            content_type = "application/json"
            response_body = _friendly_error(status_code, raw)
            print(f"[Logora] Upstream {status_code} (sanitisé)")
        except Exception as e:
            response_body = json.dumps({
                "error": {"type": "network", "message": "Erreur réseau côté moteur Logora."}
            }).encode("utf-8")
            status_code = 502
            content_type = "application/json"
            print(f"[Logora] Erreur proxy : {e}")

        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(response_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Powered-By", "Logora")
        self.end_headers()
        self.wfile.write(response_body)

    # ------------------------------------------------------------------
    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    with ThreadedServer(("", PORT), LogoraHandler) as httpd:
        print()
        print("  ════════════════════════════════════════════════════════")
        print("    Logora — Démo de modération")
        print("  ════════════════════════════════════════════════════════")
        print(f"    Ouvre :   http://localhost:{PORT}")
        print(f"    Arrêter : Ctrl+C")
        print("  ════════════════════════════════════════════════════════")
        print()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Serveur arrêté.")
