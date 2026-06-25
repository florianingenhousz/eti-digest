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


CA_MIN = 20_000_000
CA_MAX = 200_000_000
PAPPERS_CALLS_MAX = 30


def check_pappers(siren):
    """Returns (ca_millions, effectif) or (None, None) if unavailable."""
    if not PAPPERS_API_KEY or not siren:
        return None, None
    try:
        params = urllib.parse.urlencode({
            "api_token": PAPPERS_API_KEY,
            "siren": siren,
        })
        url = "https://api.pappers.fr/v2/entreprise?" + params
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        ca = data.get("chiffre_affaires")
        effectif = data.get("effectif") or data.get("tranche_effectif_salarie")
        ca_m = round(ca / 1_000_000, 1) if ca else None
        if ca_m:
            print(f"    Pappers {siren}: {ca_m}M€ CA")
        else:
            print(f"    Pappers {siren}: pas de CA ({data.get('denomination', '?')})")
        return ca_m, effectif
    except urllib.error.HTTPError as e:
        print(f"    Pappers {siren}: HTTP {e.code}")
        return None, None
    except Exception as ex:
        print(f"    Pappers {siren}: erreur {ex}")
        return None, None


def filter_with_pappers(events):
    """Enrich events with CA from Pappers, filter out confirmed non-ETIs."""
    if not PAPPERS_API_KEY:
        return events

    calls = 0
    filtered = []
    for e in events:
        if calls >= PAPPERS_CALLS_MAX:
            # Keep remaining without validation rather than silently dropping them
            filtered.append(e)
            continue
        siren = e.get("siren", "")
        if not siren:
            filtered.append(e)
            continue
        ca, effectif = check_pappers(siren)
        calls += 1
        if ca is not None:
            if CA_MIN <= ca * 1_000_000 <= CA_MAX:
                e["ca"] = ca
                e["effectif"] = effectif
                filtered.append(e)
            # else: confirmed non-ETI → drop silently
        else:
            # No Pappers data → keep, Claude will judge
            filtered.append(e)

    print(f"  Pappers: {calls} calls, {len(filtered)}/{len(events)} events kept")
    return filtered


def build_digest(bodacc_events, rss_articles):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    def fmt_event(e):
        ca_str = str(e.get("ca", "")) + "M€ CA" if e.get("ca") else "CA non vérifié"
        return "- [{}] {} ({}) | {} | SIREN {} : {}".format(
            e.get("type", ""), e.get("company", ""), e.get("city", ""),
            ca_str, e.get("siren", ""), e.get("content", "")
        )

    bodacc_text = "\n".join(fmt_event(e) for e in bodacc_events) or "Aucune annonce Bodacc aujourd’hui."
    rss_text = "\n".join(
        "- [{}] {} - {}".format(e.get("source", ""), e.get("title", ""), e.get("summary", ""))
        for e in rss_articles
    ) or "Aucun article presse aujourd’hui."

    prompt = f"""Tu es un expert en développement commercial B2B ciblant les ETI françaises.

Voici les signaux du jour. Les entreprises listées ont été pré-filtrées : celles avec un CA vérifié sont dans la fourchette 20-200M€. Les autres ont un CA non vérifié.

## Annonces Bodacc (24 dernières heures)
{bodacc_text}

## Presse spécialisée
{rss_text}

Sélectionne les 3 à 5 meilleures opportunités de prospection parmi ces signaux.

Critères : moment de vie fort (transmission, cession, procédure collective, fusion, changement de dirigeant), fenêtre de prospection ouverte, entreprise de taille ETI.

IMPORTANT : réponds UNIQUEMENT avec les blocs ETI, sans introduction ni conclusion. Format strict :

*[Emoji] [Nom entreprise]* — [Ville] | [CA]M€
Signal : [4-6 mots]
Contexte : [1 phrase]
Opportunité : [1 phrase]

Sépare chaque bloc par "---SPLIT---" seul sur sa ligne. Apostrophes droites uniquement (‘).
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )

    text = message.content[0].text
    text = text.replace("’", "’").replace("‘", "’")
    return text


def send_whatsapp(message):
    encoded = urllib.parse.quote(message)
    url = (
        f"https://api.callmebot.com/whatsapp.php"
        f"?phone={CALLMEBOT_PHONE}&text={encoded}&apikey={CALLMEBOT_APIKEY}"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="ignore")[:200]
            print(f"  CallMeBot: {resp.status} — {body}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:200]
        print(f"  CallMeBot error: {e.code} — {body}")


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

    print("Filtering with Pappers...")
    bodacc_events = filter_with_pappers(bodacc_events)

    print("Building digest with Claude...")
    digest = build_digest(bodacc_events, rss_articles)

    blocks = [b.strip() for b in digest.split("---SPLIT---") if b.strip()]
    date_str = datetime.now().strftime("%d %B %Y")
    header = f"\U0001f3af *ETI du {date_str}* — {len(blocks)} opportunités"

    print(f"Sending {len(blocks) + 1} WhatsApp messages...")
    send_whatsapp(header)

    for i, block in enumerate(blocks):
        time.sleep(15)
        print(f"  [{i+1}/{len(blocks)}] {block[:60]}...")
        send_whatsapp(block)

    print("Done.")


if __name__ == "__main__":
    main()
