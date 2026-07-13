"""
find_official_links.py
-----------------------
Lit le road book markdown généré par gpxcities.py (nom déduit de gpx_file
dans parameters.py : "<nom_gpx>_road_book.md" dans output_folder) et complète
les communes qui n'ont pas encore de lien avec :
  1. leur site officiel (propriété Wikidata P856 "site officiel"), si disponible ;
  2. sinon, leur page Wikipédia (fr) en repli.

Les communes portent parfois le même nom que d'autres communes françaises
(homonymes, ex. plusieurs "Fons" en France). Pour désambiguïser, le script :
  1. repère d'abord toutes les communes non ambiguës du parcours (un seul
     lieu plausible dans Wikidata) comme points d'ancrage géographiques
     fiables (coordonnées P625) ;
  2. pour chaque commune ambiguë, cherche l'ancre fiable la plus proche
     AVANT et APRÈS sur la trace (pas seulement le voisin immédiat, qui
     peut lui-même être ambigu) ;
  3. choisit l'homonyme dont les coordonnées sont les plus proches de ces
     deux ancres, plutôt qu'un premier résultat de recherche arbitraire.

Génère aussi une page HTML autonome (pas d'images, CSS inline) directement
publiable : output_folder/<gpx_file>_road_book.html.

Utilise les mêmes dossiers que le reste du projet (parameters.py) et le
cache_manager.py existant (pickle), exactement comme gpxcities.py / osm_tools.py.

Usage:
    python find_official_links.py [chemin_vers_le_road_book.md]

Si aucun chemin n'est fourni, le script déduit le fichier d'entrée à partir
de gpx_file (parameters.py) : output_folder/<gpx_file>_road_book.md.
"""

import os
import re
import sys
import time
import html
import math
import requests

from parameters import output_folder, gpx_file
import cache_manager as cache

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
HEADERS = {"User-Agent": "TourmagneLinkFinder/1.0 (contact: tc@tcrouzet.com)"}
REQUEST_PAUSE = 0.3  # secondes entre deux requêtes réseau, pour rester poli avec l'API

LINE_RE = re.compile(r"^(km\d+)\s*-\s*(.+?)\s+([\d.]+km)\s*$")
LINK_RE = re.compile(r"^\[(?P<name>.+?)\]\((?P<url>.+?)\)$")
TITLE_RE = re.compile(r"^#\s+(.+)$")
COUNT_RE = re.compile(r"^(\d+\s+communes)\s*$")

cache.init_cache("villes_links")


# ----------------------------------------------------------------------------
# Parsing du markdown
# ----------------------------------------------------------------------------

def parse_line(line):
    """
    Découpe une ligne "kmXXX - Nom Xkm" ou "kmXXX - [Nom](url) Xkm".
    Retourne (km_label, name, url_ou_None, dist_label) ou None si la ligne
    ne correspond pas au motif attendu.
    """
    m = LINE_RE.match(line.strip())
    if not m:
        return None

    km_label, body, dist_label = m.groups()
    link_match = LINK_RE.match(body.strip())
    if link_match:
        return km_label, link_match.group("name"), link_match.group("url"), dist_label
    return km_label, body.strip(), None, dist_label


# ----------------------------------------------------------------------------
# Wikidata : recherche brute (mise en cache par nom)
# ----------------------------------------------------------------------------

def wikidata_search(name):
    """Recherche les entités Wikidata correspondant au nom donné (fr)."""
    params = {
        "action": "wbsearchentities",
        "search": name,
        "language": "fr",
        "uselang": "fr",
        "type": "item",
        "limit": 10,
        "format": "json",
    }
    r = requests.get(WIKIDATA_API, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json().get("search", [])


def wikidata_get_entity(qid):
    """Récupère les infos utiles (P856, P625, P17, sitelinks) d'une entité."""
    params = {
        "action": "wbgetentities",
        "ids": qid,
        "props": "claims|sitelinks|labels",
        "languages": "fr",
        "format": "json",
    }
    r = requests.get(WIKIDATA_API, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json().get("entities", {}).get(qid, {})
    return data


def get_candidates_entities(name):
    """
    Renvoie la liste brute des entités Wikidata candidates pour ce nom.
    Mis en cache par nom (indépendant du contexte géographique), puisque la
    recherche brute Wikidata ne dépend pas de la position sur la trace.
    """
    hash_key = cache.create_hash(name, "wikidata_raw_entities_v1")
    found, cached = cache.get_cache(hash_key)
    if found:
        return cached

    entities = []
    try:
        candidates = wikidata_search(name)
        time.sleep(REQUEST_PAUSE)
        for cand in candidates:
            entity = wikidata_get_entity(cand["id"])
            time.sleep(REQUEST_PAUSE)
            entities.append(entity)
    except requests.RequestException as e:
        print(f"  ! Erreur réseau pour '{name}': {e}")

    cache.into_cache(hash_key, entities)
    return entities


def extract_official_site(entity):
    claims = entity.get("claims", {})
    p856 = claims.get("P856")
    if not p856:
        return None
    try:
        return p856[0]["mainsnak"]["datavalue"]["value"]
    except (KeyError, IndexError):
        return None


def extract_wikipedia_url(entity):
    sitelinks = entity.get("sitelinks", {})
    frwiki = sitelinks.get("frwiki")
    if not frwiki:
        return None
    title = frwiki.get("title")
    if not title:
        return None
    return "https://fr.wikipedia.org/wiki/" + title.replace(" ", "_")


def extract_coordinates(entity):
    """Renvoie (latitude, longitude) depuis P625, ou None."""
    claims = entity.get("claims", {})
    p625 = claims.get("P625")
    if not p625:
        return None
    try:
        value = p625[0]["mainsnak"]["datavalue"]["value"]
        return value["latitude"], value["longitude"]
    except (KeyError, IndexError, TypeError):
        return None


def is_french_place(entity):
    """
    Vérifie grossièrement que l'entité est bien un lieu situé en France
    (propriété P17 = Q142).
    """
    claims = entity.get("claims", {})
    p17 = claims.get("P17")
    if p17:
        for statement in p17:
            try:
                if statement["mainsnak"]["datavalue"]["value"]["id"] == "Q142":
                    return True
            except (KeyError, TypeError):
                continue
    return False


def haversine_km(coord1, coord2):
    """Distance approximative en km entre deux points (lat, lon)."""
    lat1, lon1 = coord1
    lat2, lon2 = coord2
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ----------------------------------------------------------------------------
# Désambiguïsation géographique (homonymes)
# ----------------------------------------------------------------------------

ANCHOR_SPREAD_KM = 5.0  # candidats considérés comme "le même endroit" si <5km d'écart


def get_pool(name):
    """Liste des candidats Wikidata retenus pour ce nom (France si possible)."""
    entities = get_candidates_entities(name)
    french = [e for e in entities if is_french_place(e)]
    return french if french else entities


def resolve_town_links(name, entity):
    """Extrait (site officiel, page Wikipédia) pour une entité choisie."""
    if entity is None:
        return None, None
    return extract_official_site(entity), extract_wikipedia_url(entity)


# ----------------------------------------------------------------------------
# Traitement du fichier
# ----------------------------------------------------------------------------

def find_input_file(explicit_path=None):
    """
    Déduit le nom du fichier villes à partir du gpx_file déclaré dans
    parameters.py, en suivant la même convention que gpxcities.py :
    "<nom_gpx>_road_book.md" dans output_folder.
    """
    if explicit_path:
        return explicit_path

    road_book = os.path.join(output_folder, gpx_file.replace(".gpx", "_road_book.md"))
    if os.path.exists(road_book):
        return road_book

    raise FileNotFoundError(
        f"{road_book} introuvable. Générez d'abord le road book avec "
        "gpxcities.py, ou précisez un chemin en argument."
    )


def parse_full_md(path):
    """Relit le fichier final (titre, ligne de comptage, entrées kmXXX)."""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    title, count_line, entries = None, None, []
    for line in lines:
        stripped = line.strip()

        if title is None:
            m = TITLE_RE.match(stripped)
            if m:
                title = m.group(1).strip()
                continue

        if count_line is None:
            m = COUNT_RE.match(stripped)
            if m:
                count_line = m.group(1)
                continue

        parsed = parse_line(line)
        if parsed:
            entries.append(parsed)

    return title, count_line, entries


def generate_html(md_path, html_path):
    """
    Génère une page HTML autonome (CSS inline, pas d'images, pas de
    dépendance externe) directement publiable, à partir du road book final.
    """
    title, count_line, entries = parse_full_md(md_path)

    rows = []
    for km_label, name, url, dist_label in entries:
        safe_name = html.escape(name)
        if url:
            town_html = (
                f'<a href="{html.escape(url)}" target="_blank" rel="noopener">'
                f"{safe_name}</a>"
            )
        else:
            town_html = safe_name

        rows.append(
            "<li>"
            f'<span class="km">{html.escape(km_label)}</span>'
            f'<span class="town">{town_html}</span>'
            f'<span class="dist">{html.escape(dist_label)}</span>'
            "</li>"
        )

    doc = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title or "Road book")}</title>
<style>
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
    background: #f7f5f0;
    color: #222;
    margin: 0;
    padding: 2rem 1rem;
  }}
  .container {{ max-width: 720px; margin: 0 auto; }}
  h1 {{ font-size: 1.6rem; margin: 0 0 0.2rem; }}
  .count {{ color: #666; margin: 0 0 1.5rem; }}
  ul {{ list-style: none; padding: 0; margin: 0; border-top: 1px solid #ddd; }}
  li {{
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
    padding: 0.5rem 0;
    border-bottom: 1px solid #eee;
  }}
  .km {{ font-weight: 600; color: #a33; min-width: 4.5rem; }}
  .town {{ flex: 1; }}
  .town a {{ color: #1a5fb4; text-decoration: none; }}
  .town a:hover {{ text-decoration: underline; }}
  .dist {{ color: #888; font-size: 0.9rem; white-space: nowrap; }}
  footer {{ margin-top: 2rem; color: #999; font-size: 0.8rem; text-align: center; }}
</style>
</head>
<body>
<div class="container">
  <h1>{html.escape(title or "")}</h1>
  <p class="count">{html.escape(count_line or "")}</p>
  <ul>
    {''.join(rows)}
  </ul>
  <footer>Généré automatiquement — liens officiels ou Wikipédia via Wikidata.</footer>
</div>
</body>
</html>
"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(doc)

    print(f"Page HTML générée : {html_path}")


def process_file(input_path, output_path):
    with open(input_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # records[i] est soit None (ligne non pertinente), soit (km_label, name, url, dist_label)
    records = [parse_line(raw) for raw in lines]
    town_indices = [i for i, r in enumerate(records) if r is not None]
    position_of = {idx: pos for pos, idx in enumerate(town_indices)}

    # ------------------------------------------------------------------
    # Passe 1 : récupérer les candidats Wikidata de chaque commune et
    # repérer les "ancres" fiables (un seul lieu plausible, sans ambiguïté).
    # ------------------------------------------------------------------
    pools = {}
    anchors = {}  # idx -> (lat, lon), uniquement pour les communes non ambiguës
    ambiguous_indices = []

    for idx in town_indices:
        name = records[idx][1]
        print(f"Recherche des candidats pour : {name} ...")
        pool = get_pool(name)
        pools[idx] = pool

        coords_list = [c for c in (extract_coordinates(e) for e in pool) if c]

        if not coords_list:
            ambiguous_indices.append(idx)
            continue

        if len(coords_list) == 1:
            anchors[idx] = coords_list[0]
            continue

        # Plusieurs candidats avec coordonnées : si elles sont toutes très
        # proches, c'est probablement la même commune (doublon Wikidata),
        # donc pas un vrai homonyme géographique.
        spread = max(haversine_km(coords_list[0], c) for c in coords_list[1:])
        if spread <= ANCHOR_SPREAD_KM:
            anchors[idx] = coords_list[0]
        else:
            ambiguous_indices.append(idx)

    # ------------------------------------------------------------------
    # Passe 2 : pour chaque commune ambiguë, chercher l'ancre fiable la
    # plus proche AVANT et APRÈS sur la trace (pas seulement le voisin
    # immédiat, qui peut lui-même être ambigu), puis choisir l'homonyme
    # le plus proche géographiquement de ces deux repères.
    # ------------------------------------------------------------------
    chosen_entity = {}

    for idx in anchors:
        pool = pools[idx]
        target = anchors[idx]
        # ré-associe l'entité correspondant à l'ancre (celle dont les
        # coordonnées sont les plus proches du point retenu)
        chosen_entity[idx] = min(
            pool,
            key=lambda e: (
                haversine_km(target, extract_coordinates(e))
                if extract_coordinates(e)
                else float("inf")
            ),
        )

    for idx in ambiguous_indices:
        pos = position_of[idx]
        name = records[idx][1]

        before_coords, after_coords = None, None
        for p in range(pos - 1, -1, -1):
            aidx = town_indices[p]
            if aidx in anchors:
                before_coords = anchors[aidx]
                break
        for p in range(pos + 1, len(town_indices)):
            aidx = town_indices[p]
            if aidx in anchors:
                after_coords = anchors[aidx]
                break

        pool = pools[idx]
        scored = []
        for entity in pool:
            coords = extract_coordinates(entity)
            if coords is None:
                continue
            dist, n = 0.0, 0
            if before_coords is not None:
                dist += haversine_km(before_coords, coords)
                n += 1
            if after_coords is not None:
                dist += haversine_km(after_coords, coords)
                n += 1
            if n:
                scored.append((dist, entity))

        if scored:
            scored.sort(key=lambda x: x[0])
            chosen_entity[idx] = scored[0][1]
            print(f"  Homonymes pour '{name}' -> choix par proximité géographique")
        else:
            chosen_entity[idx] = pool[0] if pool else None
            if len(pool) > 1:
                print(
                    f"  ! '{name}' : {len(pool)} homonymes sans coordonnées "
                    "utilisables, choix arbitraire (à vérifier)."
                )

    # ------------------------------------------------------------------
    # Passe 3 : écriture des liens (cache + fichier de sortie).
    # ------------------------------------------------------------------
    new_lines = list(lines)
    found_official = 0
    found_wikipedia = 0
    not_found = 0
    missing = []

    for idx in town_indices:
        km_label, name, url, dist_label = records[idx]

        hash_key = cache.create_hash(name, f"wikidata_resolve_v3::{km_label}")
        found, cached = cache.get_cache(hash_key)
        if found:
            official_url, wiki_url = cached
        else:
            official_url, wiki_url = resolve_town_links(name, chosen_entity.get(idx))
            cache.into_cache(hash_key, (official_url, wiki_url))

        if url:
            # Déjà un lien dans le fichier d'origine : on ne le remplace pas.
            continue

        chosen_url = official_url or wiki_url
        if chosen_url:
            new_lines[idx] = f"{km_label} - [{name}]({chosen_url}) {dist_label}\n"
            if official_url:
                found_official += 1
                print(f"{name} -> officiel : {chosen_url}")
            else:
                found_wikipedia += 1
                print(f"{name} -> wikipedia : {chosen_url}")
        else:
            not_found += 1
            missing.append(name)
            print(f"{name} -> rien trouvé")

    with open(output_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    print("\n---- Résumé ----")
    print(f"Sites officiels trouvés : {found_official}")
    print(f"Pages Wikipédia trouvées : {found_wikipedia}")
    print(f"Communes sans lien       : {not_found}")
    if missing:
        print("Communes à vérifier manuellement :")
        for m in missing:
            print(f"  - {m}")

    print(f"\nFichier généré : {output_path}")


if __name__ == "__main__":
    explicit = sys.argv[1] if len(sys.argv) > 1 else None
    input_path = find_input_file(explicit)
    output_path = input_path
    process_file(input_path, output_path)

    html_path = os.path.join(output_folder, gpx_file.replace(".gpx", "_road_book.html"))
    generate_html(output_path, html_path)

    cache.close_cache()
