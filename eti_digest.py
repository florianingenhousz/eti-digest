"""
ETI Digest → WhatsApp via CallMeBot
Fetches ETI signals from Bodacc + press RSS, selects 3-5 prospects with Claude,
sends a daily prospection briefing to WhatsApp.
"""

import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone

import feedparser
import anthropic

CALLMEBOT_PHONE = os.environ["CALLMEBOT_PHONE"].strip()
CALLMEBOT_APIKEY = os.environ["CALLMEBOT_APIKEY"].strip()
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"].strip()
PAPPERS_API_KEY = os.environ.get("PAPPERS_API_KEY", "").strip()

BODACC_BASE = "https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets"

RSS_FEEDS = [
    ("Les Echos", "https://www.lesechos.fr/rss/rss_finance.xml"),
    ("L'Usine Nouvelle", "https://www.usinenouvelle.com/flux-rss/actualite/flux.xml"),
    ("BFM Business", "https://bfmbusiness.bfmtv.com/rss/info/flux-rss/flux-toutes-les-actualites/"),
]

ETI_SIGNAL_WORDS = {
    "cession", "transmission", "rachat", "acquisition", "reprise",
    "redressement", "liquidation", "sauvegarde", "restructur",
    "dirigeant", "pdg", "directeur général", "président",
    "actionnaire", "capital", "lbo", "private equity", "fonds d'investissement",
    "fusion", "rapprochement", "nouveau directeur", "nouveau président",
}


def _extract_bodacc_record(record):
    name = record.get("commercant") or ""
    registre = record.get("registre") or []
    siren = registre[0].replace(" ", "") if registre else ""
    city = record.get("ville") or ""
    famille = record.get("familleavis_lib") or record.get("familleavis") or ""
    content = record.get("acte") or record.get("jugement") or record.get("modificationsgenerales") or ""
    if isinstance(content, dict):
        content = json.dumps(content, ensure_ascii=False)
    return name.strip(), siren, city.strip(), famille, str(content)[:300]


def fetch_bodacc_events():
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d")
    events = []

    # familleavis: vente=cessions, collective=procédures collectives, conciliation=difficulté
    where = f"dateparution >= date'{since}' AND (familleavis='vente' OR familleavis='collective' OR familleavis='conciliation')"

    try:
        params = urllib.parse.urlencode({
            "where": where,
            "limit": 80,
            "order_by": "dateparution DESC",
            "select": "commercant,ville,registre,familleavis,familleavis_lib,dateparution,acte,jugement",
        })
        url = f"{BODACC_BASE}/annonces-commerciales/records?{params}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        for record in data.get("results", []):
            name, siren, city, famille, content = _extract_bodacc_record(record)
            if not name:
                continue
            events.append({
                "type": famille,
                "company": name,
                "siren": siren,
                "city": city,
                "date": record.get("dateparution", ""),
                "content": content,
            })
    except Exception as e:
        print(f"  Bodacc error: {e}")

    return events


def fetch_rss_news():
    articles = []
    for source_name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:40]:
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                combined = (title + " " + summary).lower()
                if any(word in combined for word in ETI_SIGNAL_WORDS):
                    articles.append({
                        "source": source_name,
                        "title": title,
                        "summary": summary[:400],
                        "date": entry.get("published", ""),
                    })
        except Exception as e:
            print(f"  RSS {source_name} error: {e}")
    return articles


def check_pappers(siren):
    if not PAPPERS_API_KEY or not siren:
        return None
    try:
        params = urllib.parse.urlencode({"api_token": PAPPERS_API_KEY, "siren": siren})
        url = f"https://api.pappers.fr/v2/entreprise?{params}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        ca = data.get("chiffre_affaires")
        if ca:
            return round(ca / 1_000_000, 1)
    except Exception:
        pass
    return None


def build_digest(bodacc_events, rss_articles):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    bodacc_text = "\n".join(
        f"- [{e['type']}] {e['company']} ({e['city']}, SIREN {e['siren']}) : {e['content']}"
        for e in bodacc_events
    ) or "Aucune annonce Bodacc aujourd'hui."

    rss_text = "\n".join(
        f"- [{e['source']}] {e['title']} — {e['summary']}"
        for e in rss_articles
    ) or "Aucun article presse aujourd'hui."

    prompt = f"""Tu es un expert en développement commercial B2B ciblant les ETI françaises (CA entre 20 et 200M€).

Voici les signaux du jour :

## Annonces Bodacc (24 dernières heures)
{bodacc_text}

## Presse spécialisée (24 dernières heures)
{rss_text}

Sélectionne 3 à 5 ETI qui traversent un moment de vie important et représentent une opportunité de prospection commerciale prioritaire.

Moments de vie cibles : transmission, cession, rachat, redressement judiciaire, changement de dirigeant, changement d'actionnaire, fusion-acquisition.

Critères de sélection :
- CA estimé entre 20M€ et 200M€ (exclure les grands groupes et micro-PME)
- Moment de vie clairement identifiable et récent (max 48h)
- Fenêtre de prospection ouverte (nouveau décideur, nouveau contexte, besoin de conseil)

Pour chaque ETI retenue, génère un bloc avec ce format exact (apostrophes droites uniquement) :

*[Emoji] [Nom entreprise]* — [Ville] | ~[CA estimé]M€
Signal : [type de moment en 4-6 mots]
Contexte : [1 phrase factuelle sur ce qui se passe]
Pourquoi prospecter : [1 phrase sur la fenêtre d'opportunité]

Sépare chaque bloc par une ligne contenant uniquement "---SPLIT---".

Si les données sont insuffisantes pour identifier des ETIs qualifiées, dis-le clairement plutôt que de forcer des résultats.
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )

    text = message.content[0].text
    text = text.replace("’", "'").replace("‘", "'")
    return text


def send_whatsapp(message):
    encoded = urllib.parse.quote(message)
    url = (
        f"https://api.callmebot.com/whatsapp.php"
        f"?phone={CALLMEBOT_PHONE}&text={encoded}&apikey={CALLMEBOT_APIKEY}"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            print(f"  CallMeBot: {resp.status} ({len(message)} chars)")
    except urllib.error.HTTPError as e:
        print(f"  CallMeBot error: {e.code} — skipping")


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching Bodacc events...")
    bodacc_events = fetch_bodacc_events()
    print(f"  {len(bodacc_events)} events")

    print("Fetching RSS news...")
    rss_articles = fetch_rss_news()
    print(f"  {len(rss_articles)} relevant articles")

    if not bodacc_events and not rss_articles:
        print("No data — skipping.")
        return

    print("Building digest with Claude...")
    digest = build_digest(bodacc_events, rss_articles)

    blocks = [b.strip() for b in digest.split("---SPLIT---") if b.strip()]
    date_str = datetime.now().strftime("%d %B %Y")
    header = f"\U0001f3af *ETI du {date_str}* — {len(blocks)} opportunités"

    print(f"Sending {len(blocks) + 1} WhatsApp messages...")
    send_whatsapp(header)

    for i, block in enumerate(blocks):
        time.sleep(10)
        print(f"  [{i+1}/{len(blocks)}] {block[:60]}...")
        send_whatsapp(block)

    print("Done.")


if __name__ == "__main__":
    main()
