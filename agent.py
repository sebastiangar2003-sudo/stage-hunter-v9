#!/usr/bin/env python3
"""
Stage Hunter v8 — Agent multi-clients SaaS
Lit les clients depuis Supabase, tourne pour chacun d'eux chaque matin
"""

import os, re, time, datetime, schedule, urllib.request, urllib.parse, json
from groq import Groq
import sendgrid
from sendgrid.helpers.mail import Mail

# ─── CONFIG ──────────────────────────────────────────────────────────────────
GROQ_API_KEY     = os.environ["GROQ_API_KEY"]
SENDGRID_API_KEY = os.environ["SENDGRID_API_KEY"]
SERP_API_KEY     = os.environ["SERP_API_KEY"]
SUPABASE_URL     = os.environ["SUPABASE_URL"]
SUPABASE_KEY     = os.environ["SUPABASE_KEY"]
SCAN_HOUR        = os.environ.get("SCAN_HOUR", "08:00")
FROM_EMAIL       = os.environ.get("FROM_EMAIL", "noreply@stagehunter.ch")
# ─────────────────────────────────────────────────────────────────────────────

groq_client = Groq(api_key=GROQ_API_KEY)

def log(msg):
    print(f"[{datetime.datetime.now().strftime('%d.%m.%Y %H:%M:%S')}] {msg}", flush=True)


# ── SUPABASE ──────────────────────────────────────────────────────────────────

def supabase_get(table: str, params: dict = None) -> list:
    """Lit des données dans Supabase."""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"Erreur Supabase GET {table}: {e}")
        return []

def supabase_post(table: str, data: dict) -> bool:
    """Insère des données dans Supabase."""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 201)
    except Exception as e:
        log(f"Erreur Supabase POST {table}: {e}")
        return False

def get_clients_actifs() -> list:
    """Récupère tous les clients actifs depuis Supabase."""
    return supabase_get("clients", {"actif": "eq.true", "select": "*"})

def offre_deja_envoyee(client_id: str, url: str) -> bool:
    """Vérifie si on a déjà envoyé cette offre à ce client."""
    res = supabase_get("offres_envoyees", {
        "client_id": f"eq.{client_id}",
        "offre_url": f"eq.{url}"
    })
    return len(res) > 0

def marquer_offre_envoyee(client_id: str, offre: dict):
    """Enregistre l'offre dans Supabase pour éviter les doublons."""
    supabase_post("offres_envoyees", {
        "client_id": client_id,
        "offre_url": offre["url"],
        "offre_titre": offre["titre"],
        "entreprise": offre["entreprise"],
        "lettre_envoyee": True
    })


# ── RECHERCHE OFFRES ──────────────────────────────────────────────────────────

def recherche_google(query: str) -> list:
    """Fait une vraie recherche Google via SerpAPI."""
    try:
        params = urllib.parse.urlencode({
            "q": query,
            "api_key": SERP_API_KEY,
            "engine": "google",
            "num": 5,
            "hl": "fr",
            "gl": "ch",
            "tbs": "qdr:w"
        })
        url = f"https://serpapi.com/search?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("organic_results", [])
    except Exception as e:
        log(f"Erreur SerpAPI: {e}")
        return []

def detecter_langue(titre: str, desc: str) -> str:
    texte = (titre + " " + desc).lower()
    if any(w in texte for w in ["praktikum", "wir suchen", "stellenangebot", "bewerben"]):
        return "de"
    elif any(w in texte for w in ["internship", "we are looking", "join our team", "apply now"]):
        return "en"
    return "fr"

def detecter_domaine(titre: str, desc: str) -> str:
    texte = (titre + " " + desc).lower()
    if any(w in texte for w in ["comptab", "accounting", "audit", "buchhalter"]):
        return "Comptabilité"
    elif any(w in texte for w in ["marketing", "communication", "digital", "social media"]):
        return "Marketing"
    elif any(w in texte for w in ["rh", "ressources humaines", "human resources", "recrutement"]):
        return "Ressources Humaines"
    elif any(w in texte for w in ["juridique", "droit", "legal", "lawyer"]):
        return "Juridique"
    elif any(w in texte for w in ["informatique", "développeur", "software", "developer", "it "]):
        return "Informatique"
    return "Finance"

def extraire_entreprise(titre: str, source: str) -> str:
    parts = re.split(r'\s*[-|–]\s*', titre)
    if len(parts) >= 2:
        return parts[-1].strip()
    if source:
        return source.strip()
    return "Entreprise"

def chercher_offres_client(client: dict) -> list:
    """Cherche des offres personnalisées pour un client."""
    domaines = client.get("domaines", ["Finance"])
    villes   = client.get("villes", ["Genève"])
    log(f"  Recherche pour {client['nom']} — {domaines} — {villes}")

    offres    = []
    seen_urls = set()

    # Construire les requêtes dynamiquement selon les préférences du client
    villes_str = " ".join(villes)
    requetes = []
    for domaine in domaines:
        requetes.append((
            f"stage {domaine.lower()} {villes_str} site:linkedin.com OR site:jobup.ch OR site:jobs.ch",
            domaine, "fr"
        ))
        requetes.append((
            f"{domaine.lower()} internship {villes_str} site:linkedin.com OR site:jobs.ch",
            domaine, "en"
        ))

    for query, domaine_defaut, langue_defaut in requetes:
        resultats = recherche_google(query)
        for r in resultats:
            titre = r.get("title", "").strip()
            url   = r.get("link", "").strip()
            desc  = r.get("snippet", "").strip()

            if not titre or not url or url in seen_urls:
                continue

            texte     = (titre + " " + desc).lower()
            est_stage = any(w in texte for w in ["stage", "internship", "praktikum", "stagiaire"])
            est_local = any(v.lower() in texte for v in villes)

            if not est_stage or not est_local:
                continue

            # Vérifier qu'on n'a pas déjà envoyé cette offre à ce client
            if offre_deja_envoyee(client["id"], url):
                log(f"  Skip doublon: {url[:60]}")
                continue

            seen_urls.add(url)

            if "linkedin.com" in url:   plateforme = "LinkedIn"
            elif "jobup.ch" in url:     plateforme = "JobUp"
            elif "jobs.ch" in url:      plateforme = "Jobs.ch"
            elif "indeed" in url:       plateforme = "Indeed"
            else:                       plateforme = "Web"

            offres.append({
                "titre":       titre,
                "entreprise":  extraire_entreprise(titre, r.get("source", "")),
                "lieu":        next((v for v in villes if v.lower() in texte), villes[0]),
                "domaine":     detecter_domaine(titre, desc),
                "langue":      detecter_langue(titre, desc),
                "plateforme":  plateforme,
                "url":         url,
                "description": desc[:300],
                "date":        datetime.datetime.now().strftime("%Y-%m-%d")
            })

        time.sleep(0.3)

    log(f"  {len(offres)} offres nouvelles trouvées")
    return offres[:8]


# ── LETTRES DE MOTIVATION ─────────────────────────────────────────────────────

def generer_lettre(offre: dict, client: dict) -> str:
    """Génère une lettre personnalisée pour un client."""
    langue_code = offre.get("langue", "fr")
    lang_map    = {"fr": "French", "en": "English", "de": "German", "it": "Italian"}
    langue_nom  = lang_map.get(langue_code, "French")

    salutation_map = {
        "fr": "Madame, Monsieur,",
        "en": "Dear Hiring Manager,",
        "de": "Sehr geehrte Damen und Herren,",
        "it": "Gentile Responsabile,"
    }
    salutation = salutation_map.get(langue_code, "Madame, Monsieur,")

    # Profil CV du client (stocké dans Supabase ou générique)
    cv_texte = client.get("cv_texte") or f"Name: {client['nom']}\nEmail: {client['email']}"

    log(f"  Lettre {langue_nom} pour {offre['entreprise']}")

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": f"You are a professional cover letter writer for the Swiss job market. You MUST write EXCLUSIVELY in {langue_nom}. Every single word must be in {langue_nom}. This is absolutely mandatory."
                },
                {
                    "role": "user",
                    "content": f"""Write a professional internship cover letter in {langue_nom} only.

MANDATORY: Start with exactly: "{salutation}"

Candidate profile:
{cv_texte}

Position: {offre['titre']}
Company: {offre['entreprise']} ({offre['lieu']}, Switzerland)
Domain: {offre['domaine']}
Job description: {offre['description']}

Requirements:
- Language: {langue_nom} EXCLUSIVELY
- Start with: "{salutation}"
- 3 paragraphs: opening (position + company), skills match, motivation
- 250-300 words, professional Swiss tone
- No address, date, subject line, or closing signature

Write the cover letter now:"""
                }
            ],
            temperature=0.6,
            max_tokens=700,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        log(f"  Erreur lettre: {e}")
        return f"Veuillez postuler directement : {offre['url']}"


# ── EMAIL ─────────────────────────────────────────────────────────────────────

def envoyer_email_client(client: dict, offres: list, lettres: list):
    """Envoie le rapport quotidien à un client."""
    date_str = datetime.datetime.now().strftime("%d.%m.%Y")
    nom      = client["nom"].split()[0]  # Prénom seulement

    html_offres = ""
    for o, l in zip(offres, lettres):
        lang_flag = {"fr": "🇫🇷", "en": "🇬🇧", "de": "🇩🇪", "it": "🇮🇹"}.get(o.get("langue", "fr"), "🌐")
        lang_name = {"fr": "Français", "en": "English", "de": "Deutsch", "it": "Italiano"}.get(o.get("langue", "fr"), "FR")
        lettre_html = l.replace('\n', '<br>')
        html_offres += f"""
        <div style="background:#fff;border:1px solid #e0e0e0;border-left:4px solid #ff4d1c;border-radius:10px;padding:20px;margin-bottom:24px;">
          <h3 style="margin:0 0 4px;color:#1a1a1a;font-size:16px;">{o['titre']}</h3>
          <p style="margin:0 0 12px;color:#888;font-size:13px;">
            🏢 <strong>{o['entreprise']}</strong> &nbsp;·&nbsp;
            📍 {o['lieu']} &nbsp;·&nbsp;
            {o['plateforme']} &nbsp;·&nbsp;
            {lang_flag} {lang_name} &nbsp;·&nbsp;
            <span style="background:#fff3f0;color:#ff4d1c;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600;">{o['domaine']}</span>
          </p>
          <p style="margin:0 0 16px;color:#555;font-size:14px;line-height:1.6;">{o['description']}</p>
          <a href="{o['url']}" style="display:inline-block;background:#ff4d1c;color:#fff;padding:9px 20px;border-radius:8px;text-decoration:none;font-size:13px;font-weight:600;margin-bottom:20px;">Voir l'offre →</a>
          <div style="background:#f8f9fa;border-radius:8px;padding:18px;font-size:13px;color:#333;line-height:2;border:1px solid #eee;">
            <p style="margin:0 0 10px;font-weight:700;color:#ff4d1c;font-size:11px;text-transform:uppercase;letter-spacing:0.08em;">✉️ Lettre de motivation — {lang_flag} {lang_name}</p>
            {lettre_html}
          </div>
        </div>"""

    html = f"""<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f4f0;margin:0;padding:0;">
      <div style="max-width:680px;margin:0 auto;padding:32px 20px;">
        <div style="text-align:center;margin-bottom:28px;">
          <h1 style="font-size:26px;font-weight:700;color:#1a1a1a;margin:0 0 8px;">🔍 Stage Hunter</h1>
          <p style="color:#888;font-size:14px;margin:0;">Rapport quotidien · {client['nom']} · {date_str}</p>
        </div>
        <div style="background:#fff3f0;border:1px solid #ffd5cc;border-radius:10px;padding:16px 20px;margin-bottom:28px;">
          Bonjour {nom} ! ✅ <strong style="color:#c0380e;">{len(offres)} nouvelles offres</strong> trouvées pour toi aujourd'hui.
          <span style="color:#ff4d1c;font-size:14px;"> — Lettres personnalisées dans la langue de chaque offre</span>
        </div>
        {html_offres}
        <div style="text-align:center;margin-top:32px;padding-top:20px;border-top:1px solid #e5e5e5;">
          <p style="color:#bbb;font-size:12px;margin:0;">Stage Hunter · Prochain scan demain à {SCAN_HOUR}</p>
          <p style="color:#bbb;font-size:11px;margin:4px 0 0;">Pour modifier tes préférences, réponds à cet email.</p>
        </div>
      </div>
    </body></html>"""

    try:
        sg      = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)
        message = Mail(
            from_email=FROM_EMAIL,
            to_emails=client["email"],
            subject=f"[Stage Hunter] {len(offres)} nouvelles offres pour toi — {date_str}",
            html_content=html
        )
        sg.send(message)
        log(f"  ✅ Email envoyé à {client['email']}")
    except Exception as e:
        log(f"  ❌ Erreur email {client['email']}: {e}")


# ── SCAN COMPLET ──────────────────────────────────────────────────────────────

def scan_complet():
    log("=" * 60)
    log(f"SCAN QUOTIDIEN DÉMARRÉ — {datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}")
    log("=" * 60)

    clients = get_clients_actifs()
    log(f"{len(clients)} client(s) actif(s) trouvé(s)")

    if not clients:
        log("Aucun client actif. Fin du scan.")
        return

    for client in clients:
        log(f"\n── Client: {client['nom']} ({client['email']}) ──")
        try:
            offres = chercher_offres_client(client)

            if not offres:
                log(f"  Aucune nouvelle offre pour {client['nom']} aujourd'hui.")
                continue

            lettres = []
            for offre in offres:
                lettre = generer_lettre(offre, client)
                lettres.append(lettre)
                time.sleep(1)

            envoyer_email_client(client, offres, lettres)

            # Marquer toutes les offres comme envoyées dans Supabase
            for offre in offres:
                marquer_offre_envoyee(client["id"], offre)

        except Exception as e:
            log(f"  ❌ Erreur pour {client['nom']}: {e}")

        time.sleep(2)  # Pause entre chaque client

    log("\n" + "=" * 60)
    log("✅ SCAN TERMINÉ")
    log("=" * 60)


# ── BOUCLE PRINCIPALE ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    log("Stage Hunter v8 — Mode SaaS multi-clients")
    log(f"Supabase: {SUPABASE_URL}")
    log(f"Scan quotidien à: {SCAN_HOUR}")

    scan_complet()

    schedule.every().day.at(SCAN_HOUR).do(scan_complet)
    log(f"\nAgent en attente — prochain scan automatique demain à {SCAN_HOUR}")

    while True:
        schedule.run_pending()
        time.sleep(60)
