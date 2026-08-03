"""
books.py
--------
Génère les road books à partir du GPX et des données OpenStreetMap :
  - "<nom_gpx>_road_book.md"       (format "kmXXX - INSEE Nom Xkm")
  - "<nom_gpx>_road_book_plus.md"  (format "N - kmX - Xd+ - X% - INSEE [Nom](url)" + détail des voies)

Chaque commune est précédée de son code INSEE (ajouté par osm_tools.py /
gpxcities.py à partir des données OSM). C'est un identifiant unique à 100%
par commune : plus besoin de désambiguïser des homonymes par proximité
géographique, il suffit de chercher directement ce code dans le CSV
officiel des communes. Le code INSEE est retiré du road book final
publiable, il ne sert qu'en interne à ce script.

Le script complète les communes qui n'ont pas encore de lien avec :
  1. leur site officiel ;
  2. sinon, leur page Wikipédia (fr) en repli.
Le nom est en plus systématiquement suivi d'un badge markdown
"[(w)](url_wikipedia)", même quand le lien principal est déjà un site
officiel ou un lien mis à la main dans le fichier d'origine.

Source de vérité : assets/communes-france-2025.csv (code_insee, coordonnées
de mairie, page Wikipédia exacte). Le site officiel n'y étant pas présent,
il est retrouvé via Wikidata (P856, entité retrouvée par le titre Wikipédia
exact du CSV — donc sans risque d'homonyme), sinon via le paramètre
"siteweb" de l'infobox Wikipédia de la commune.

Si une ligne n'a pas de code INSEE exploitable (road book généré avant
cette mise à jour, ou commune non trouvée dans le CSV), le script se
replie sur une recherche par nom (CSV, puis Wikidata filtré aux vraies
"communes de France"), avec désambiguïsation géographique par ancrage
comme filet de sécurité.

Les liens vers les sites officiels et Wikipédia sont intégrés pendant la
génération : il n'y a plus d'étape d'enrichissement séparée.

Les pages HTML sont générées séparément par html.py, ce qui permet de modifier
manuellement les Markdown avant leur conversion.

Usage:
    python3 books.py
"""

import os
import re
import csv
import time
import math
import unicodedata
import urllib.parse
import tempfile
from bisect import bisect_left
from collections import defaultdict

import requests
from tqdm import tqdm

from parameters import output_folder, gpx_file, gpx_path, assets_dir
import cache_manager as cache
import osm_tools as o
import tools as t

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
HEADERS = {"User-Agent": "TourmagneLinkFinder/1.0 (contact: tc@tcrouzet.com)"}
REQUEST_PAUSE = 0.3  # secondes entre deux requêtes réseau
ANCHOR_SPREAD_KM = 5.0  # candidats considérés "au même endroit" si <5km (filet de sécurité)
OSM_TOWN_TAGS = {}

INSEE_RE = r"\d{5}|2[AB]\d{3}"

# Format "road_book"      : "kmXXX - [INSEE] <body> Xkm"
LINE_RE = re.compile(
    r"^(?P<km>km\d+)\s*-\s*(?:(?P<insee>" + INSEE_RE + r")\s+)?(?P<body>.+?)\s+(?P<dist>[\d.]+km)\s*$"
)
# Format "road_book_plus" :
# "N - kmX - Xd+ - X% - [INSEE] Nom" ou
# "N - kmX - Xd+ - X% - [INSEE] [Nom](url)".
# Le kilométrage et le dénivelé restent optionnels pour relire les anciens
# fichiers.
# (gpxcities.py n'entoure de crochets que s'il y a réellement une URL,
# pour rester cohérent avec le road_book classique).
PLUS_LINE_RE = re.compile(
    r"^(?P<number>\d+)\s*-\s*(?:(?P<route_km>km\d+)\s*-\s*)?"
    r"(?:(?P<elevation_gain>\d+d\+)\s*-\s*)?"
    r"(?:(?P<slope>[\d.]+%)\s*-\s*)?"
    r"(?:(?P<insee>" + INSEE_RE + r")\s+)?(?P<body>.+)$"
)
# Un nom déjà lié : "[Nom](url)"
LINK_RE = re.compile(r"^\[(?P<name>.+?)\]\((?P<url>.*?)\)$")
# Badge wikipédia déjà présent en fin de ligne : "... [(w)](url)"
WIKI_BADGE_RE = re.compile(r"\s*\[\(w\)\]\((?P<wiki>[^)]*)\)\s*$")

# ----------------------------------------------------------------------------
# CSV officiel des communes de France (source de vérité)
# ----------------------------------------------------------------------------

COMMUNES_CSV_PATH = os.path.join(assets_dir, "communes-france-2025.csv")


def normalize_name(name):
    """Normalise un nom de commune (accents, apostrophes, tirets, casse) —
    utilisé uniquement pour le repli par nom, quand le code INSEE manque."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("’", "'")
    s = re.sub(r"[\s\-']+", " ", s)
    return s.strip()


def parse_csv_wikipedia(raw_url):
    """
    Nettoie l'URL Wikipédia fournie par le CSV (parfois préfixée par un
    indésirable "fr:", jamais encodée pour les caractères spéciaux type
    parenthèses, ce qui casse la syntaxe markdown "[texte](url)").
    Retourne (url_propre, titre_page) ou (None, None).
    """
    if not raw_url:
        return None, None
    m = re.search(r"/wiki/(.+)$", raw_url.strip())
    if not m:
        return None, None
    raw_title = urllib.parse.unquote(m.group(1))
    if raw_title.startswith("fr:"):
        raw_title = raw_title[3:]
    title = raw_title.replace("_", " ").strip()
    if not title:
        return None, None
    url = "https://fr.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
    return url, title


def parse_osm_wikipedia(value):
    """Transforme par exemple ``es:Zaragoza`` en URL Wikipédia directe."""
    if not isinstance(value, str) or ":" not in value:
        return None
    language, title = value.split(":", 1)
    if not re.fullmatch(r"[a-z-]{2,12}", language) or not title:
        return None
    slug = urllib.parse.quote(title.replace(" ", "_"))
    return f"https://{language}.wikipedia.org/wiki/{slug}"


def osm_candidate_by_name(name):
    tags = OSM_TOWN_TAGS.get(normalize_name(name))
    if not tags:
        return None
    return {
        "source": "osm",
        "coords": None,
        "wiki_url": parse_osm_wikipedia(tags.get("wikipedia")),
        "wiki_title": None,
        "entity": None,
        "insee": None,
        "label": name,
        "population": tags.get("population"),
        "website": tags.get("website"),
    }


def load_communes_csv():
    """Charge le CSV et indexe les lignes par code INSEE (clé primaire) et
    par variantes normalisées du nom (repli)."""
    insee_index = {}
    name_index = defaultdict(list)

    if not os.path.exists(COMMUNES_CSV_PATH):
        print(
            f"  ! CSV des communes introuvable ({COMMUNES_CSV_PATH}) : "
            "repli sur la recherche Wikidata pour toutes les communes."
        )
        return insee_index, name_index

    with open(COMMUNES_CSV_PATH, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            insee = (row.get("code_insee") or "").strip()
            if insee:
                insee_index[insee] = row
            for field in ("nom_standard", "nom_sans_pronom", "nom_sans_accent"):
                key = normalize_name(row.get(field))
                if key:
                    name_index[key].append(row)

    return insee_index, name_index


INSEE_INDEX, NAME_INDEX = load_communes_csv()


def csv_row_to_candidate(row):
    """Convertit une ligne CSV en candidat uniforme."""
    coords = None
    try:
        coords = (float(row["latitude_mairie"]), float(row["longitude_mairie"]))
    except (KeyError, ValueError, TypeError):
        coords = None

    wiki_url, wiki_title = parse_csv_wikipedia(row.get("url_wikipedia"))

    return {
        "source": "csv",
        "coords": coords,
        "wiki_url": wiki_url,
        "wiki_title": wiki_title,
        "entity": None,
        "insee": (row.get("code_insee") or "").strip(),
        "label": row.get("nom_standard"),
        "population": row.get("population"),
    }


def get_csv_candidates_by_name(name):
    """Repli : recherche par nom dans le CSV (utilisé seulement si le code
    INSEE de la ligne est absent ou introuvable)."""
    rows = NAME_INDEX.get(normalize_name(name), [])
    seen_insee = set()
    candidates = []
    for row in rows:
        insee = (row.get("code_insee") or "").strip()
        if insee in seen_insee:
            continue
        seen_insee.add(insee)
        candidates.append(csv_row_to_candidate(row))
    return candidates


# ----------------------------------------------------------------------------
# Parsing du markdown (les deux formats)
# ----------------------------------------------------------------------------

def split_body(body):
    """
    Découpe le corps d'une ligne en (name, url_ou_None, wiki_url_ou_None).
    Gère le badge "[(w)](url)" optionnel déjà présent (idempotence si le
    script est relancé sur un fichier déjà traité).
    """
    body = body.strip()

    wiki_url = None
    m = WIKI_BADGE_RE.search(body)
    if m:
        wiki_url = m.group("wiki") or None
        body = body[: m.start()].rstrip()

    link_match = LINK_RE.match(body)
    if link_match:
        name = link_match.group("name")
        url = link_match.group("url") or None
    else:
        name = body
        url = None

    return name, url, wiki_url


def parse_town_line(line):
    """
    Reconnaît une ligne "commune" dans l'un des deux formats de road book.
    Retourne un dict {format, km, insee, name, url, wiki_url, dist(optionnel)}
    ou None si la ligne n'est pas une ligne de commune.
    """
    stripped = line.strip()
    if not stripped:
        return None

    m = LINE_RE.match(stripped)
    if m:
        name, url, wiki_url = split_body(m.group("body"))
        return {
            "format": "road_book",
            "km": m.group("km"),
            "insee": m.group("insee"),
            "name": name,
            "url": url,
            "wiki_url": wiki_url,
            "dist": m.group("dist"),
        }

    m = PLUS_LINE_RE.match(stripped)
    if m:
        name, url, wiki_url = split_body(m.group("body"))
        return {
            "format": "plus",
            "number": m.group("number"),
            "route_km": m.group("route_km"),
            "elevation_gain": m.group("elevation_gain"),
            "slope": m.group("slope"),
            "insee": m.group("insee"),
            "name": name,
            "url": url,
            "wiki_url": wiki_url,
        }

    return None


def rebuild_line(record, official_url, wiki_url, metadata=None):
    """
    Reconstruit la ligne markdown publiable (le code INSEE, purement
    technique, n'est jamais réécrit) :
      - nom lié au lien principal (site officiel trouvé, sinon lien déjà
        présent dans le fichier d'origine, sinon rien) ;
      - badge "[(w)](wiki_url)" systématiquement ajouté derrière le nom
        dès qu'une page Wikipédia est connue.
    """
    name = record["name"]
    main_url = record["url"] or official_url

    town_md = f"[{name}]({main_url})" if main_url else name
    if wiki_url:
        town_md += f" [(w)]({wiki_url})"

    if record["format"] == "plus":
        route_km = f" - {record['route_km']}" if record.get("route_km") else ""
        elevation_gain = (
            f" - {record['elevation_gain']}" if record.get("elevation_gain") else ""
        )
        slope = f" - {record['slope']}" if record.get("slope") else ""
        line = f"{record['number']}{route_km}{elevation_gain}{slope} - {town_md}\n"
    else:
        line = f"{record['km']} - {town_md} {record['dist']}\n"
    if metadata:
        line += f"*{metadata}*\n"
    return line


# ----------------------------------------------------------------------------
# Wikidata : repli uniquement (recherche par nom, ou par titre Wikipédia exact)
# ----------------------------------------------------------------------------

def wikidata_search(name):
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
    params = {
        "action": "wbgetentities",
        "ids": qid,
        "props": "claims|sitelinks|labels",
        "languages": "fr",
        "format": "json",
    }
    r = requests.get(WIKIDATA_API, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json().get("entities", {}).get(qid, {})


def get_candidates_entities(name):
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


def get_frwiki_title(entity):
    sitelinks = entity.get("sitelinks", {})
    frwiki = sitelinks.get("frwiki")
    if not frwiki:
        return None
    return frwiki.get("title") or None


def extract_wikipedia_url(entity):
    title = get_frwiki_title(entity)
    if not title:
        return None
    slug = title.replace(" ", "_").replace("(", "%28").replace(")", "%29")
    return "https://fr.wikipedia.org/wiki/" + slug


def extract_coordinates(entity):
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


def is_commune_de_france(entity):
    """P31 = Q484170 : exclut gares, aérodromes, familles, etc. qui
    polluaient la désambiguïsation par simple filtre pays (ex. "Melun"
    renvoyant la gare de Melun)."""
    claims = entity.get("claims", {})
    p31 = claims.get("P31")
    if not p31:
        return False
    for statement in p31:
        try:
            if statement["mainsnak"]["datavalue"]["value"]["id"] == "Q484170":
                return True
        except (KeyError, TypeError):
            continue
    return False


def wikidata_get_entity_by_title(title):
    """Entité Wikidata liée à un titre Wikipédia (fr) précis : aucune
    recherche par nom, donc aucune ambiguïté d'homonyme."""
    hash_key = cache.create_hash(title, "wikidata_entity_by_frwiki_title_v1")
    found, cached = cache.get_cache(hash_key)
    if found:
        return cached

    entity = None
    try:
        params = {
            "action": "wbgetentities",
            "sites": "frwiki",
            "titles": title,
            "props": "claims|sitelinks",
            "languages": "fr",
            "format": "json",
        }
        r = requests.get(WIKIDATA_API, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        time.sleep(REQUEST_PAUSE)
        entities = r.json().get("entities", {})
        for qid, ent in entities.items():
            if qid != "-1":
                entity = ent
                break
    except requests.RequestException as e:
        print(f"  ! Erreur réseau (entité par titre Wikipédia) pour '{title}': {e}")

    cache.into_cache(hash_key, entity)
    return entity


def extract_official_site_from_wikipedia(title):
    """Repli quand ni Wikidata (P856) n'a le site officiel : lu dans le
    paramètre "siteweb" de l'infobox "Commune de France" de Wikipédia."""
    hash_key = cache.create_hash(title, "wikipedia_infobox_siteweb_v1")
    found, cached = cache.get_cache(hash_key)
    if found:
        return cached

    url = None
    try:
        params = {
            "action": "parse",
            "page": title,
            "prop": "wikitext",
            "section": 0,
            "format": "json",
        }
        r = requests.get(
            "https://fr.wikipedia.org/w/api.php", params=params, headers=HEADERS, timeout=15
        )
        r.raise_for_status()
        time.sleep(REQUEST_PAUSE)
        wikitext = r.json().get("parse", {}).get("wikitext", {}).get("*", "")

        m = re.search(r"\|\s*siteweb\s*=\s*([^\n|}]+)", wikitext)
        if m:
            candidate = m.group(1).strip()
            candidate = candidate.strip("[]").strip()
            parts = candidate.split()
            candidate = parts[0].strip() if parts else ""
            if candidate.startswith("http"):
                url = candidate
    except requests.RequestException as e:
        print(f"  ! Erreur réseau (infobox Wikipédia) pour '{title}': {e}")

    cache.into_cache(hash_key, url)
    return url


PLACE_LABELS = {
    "city": "Grande ville",
    "town": "Ville",
    "village": "Village",
    "hamlet": "Hameau",
    "suburb": "Quartier",
    "quarter": "Quartier",
}

CAPITAL_LABELS = {
    "2": "Capitale nationale",
    "4": "Capitale régionale",
    "6": "Préfecture",
    "7": "Sous-préfecture",
}

OSM_DESIGNATION_LABELS = {
    "ville d art et d histoire": "Ville d’art et d’histoire",
    "petite cite de caractere": "Petite Cité de caractère",
    "plus beaux villages de france": "Plus Beaux Villages de France",
    "plus beaux detours de france": "Plus Beaux Détours de France",
    "station classee de tourisme": "Station classée de tourisme",
}


def town_metadata(candidate):
    if candidate is None:
        return None

    details = []
    osm_tags = OSM_TOWN_TAGS.get(str(candidate.get("insee") or ""))
    if not osm_tags:
        osm_tags = OSM_TOWN_TAGS.get(normalize_name(candidate.get("label") or ""), {})

    population = osm_tags.get("population") or candidate.get("population")
    try:
        population = int(float(population))
    except (TypeError, ValueError):
        population = 0
    if population > 0:
        if population >= 10000:
            rounding = 1000
        elif population >= 1000:
            rounding = 500
        else:
            rounding = 50
        population = math.floor(population / rounding + 0.5) * rounding
        details.append(f"{population:,}".replace(",", " ") + " habitants")

    place = PLACE_LABELS.get(str(osm_tags.get("place", "")).lower())
    if place:
        details.append(place)

    capital_value = str(osm_tags.get("capital", "")).lower()
    capital = CAPITAL_LABELS.get(capital_value)
    if capital and capital not in details:
        details.append(capital)

    for status in osm_tags.get("administrative_status", []):
        if status not in details:
            details.append(status)

    designation = OSM_DESIGNATION_LABELS.get(
        normalize_name(osm_tags.get("designation", ""))
    )
    if designation and designation not in details:
        details.append(designation)

    return " · ".join(details) or None


def haversine_km(coord1, coord2):
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


def get_pool_by_name_fallback(name):
    """
    Repli complet par nom (CSV puis Wikidata filtré) — utilisé UNIQUEMENT
    quand le code INSEE de la ligne est absent ou introuvable dans le CSV.
    """
    csv_candidates = get_csv_candidates_by_name(name)
    if csv_candidates:
        return csv_candidates

    entities = get_candidates_entities(name)
    communes = [e for e in entities if is_commune_de_france(e)]
    pool_entities = communes or [e for e in entities if is_french_place(e)] or entities

    return [
        {
            "source": "wikidata",
            "coords": extract_coordinates(e),
            "wiki_url": extract_wikipedia_url(e),
            "wiki_title": get_frwiki_title(e),
            "entity": e,
            "insee": None,
            "label": name,
        }
        for e in pool_entities
    ]


def resolve_town_links(candidate):
    """
    (site officiel, page Wikipédia) pour le candidat retenu.
    Wikipédia vient directement de la source (CSV en priorité : toujours
    exact). Le site officiel :
      1. P856 de l'entité Wikidata si déjà connue (repli Wikidata) ;
      2. sinon P856 retrouvé via le titre Wikipédia exact (pas de
         recherche par nom, donc pas de risque d'homonyme) ;
      3. sinon "siteweb" de l'infobox Wikipédia de la commune.
    """
    if candidate is None:
        return None, None

    if candidate.get("source") == "osm":
        return candidate.get("website") or None, candidate.get("wiki_url")

    wiki_url = candidate.get("wiki_url")
    title = candidate.get("wiki_title")
    entity = candidate.get("entity")

    official_url = extract_official_site(entity) if entity is not None else None

    if not official_url and title:
        entity_by_title = wikidata_get_entity_by_title(title)
        if entity_by_title:
            official_url = extract_official_site(entity_by_title)

    if not official_url and title:
        official_url = extract_official_site_from_wikipedia(title)

    return official_url, wiki_url


# ----------------------------------------------------------------------------
# Génération brute des deux road books
# ----------------------------------------------------------------------------

MIN_ROAD_BOOK_SEGMENT_METERS = 100


def terrain_distance(first, second):
    """Distance entre deux types de surface selon leur ordre de roulabilité."""
    if first == second:
        return 0
    first_order = o.terrain_order(first)
    second_order = o.terrain_order(second)
    if first_order is None or second_order is None:
        return float("inf")
    return abs(first_order - second_order)


def add_way_metrics(target, source, ratio=1.0):
    target.distance += source.distance * ratio
    target.elevationPositif += source.elevationPositif * ratio
    target.elevationNegatif += source.elevationNegatif * ratio


def absorb_short_town_block(ways_info, minimum):
    """Absorbe les segments courts à l'intérieur d'une seule traversée de ville."""
    kept_indices = [index for index, way in enumerate(ways_info) if way.distance >= minimum]
    if not kept_indices:
        # Exception nécessaire pour ne jamais faire disparaître une ville :
        # sa voie la plus longue sert d'ancre et absorbe toutes les autres.
        anchor_index = max(range(len(ways_info)), key=lambda index: ways_info[index].distance)
        anchor = ways_info[anchor_index]
        for index, way in enumerate(ways_info):
            if index != anchor_index:
                add_way_metrics(anchor, way)
        return [anchor]

    kept = [ways_info[index] for index in kept_indices]
    kept_position = {source_index: target_index for target_index, source_index in enumerate(kept_indices)}

    for index, way in enumerate(ways_info):
        if way.distance >= minimum:
            continue

        insertion = bisect_left(kept_indices, index)
        previous_index = kept_indices[insertion - 1] if insertion else None
        next_index = kept_indices[insertion] if insertion < len(kept_indices) else None

        if previous_index is None:
            add_way_metrics(kept[kept_position[next_index]], way)
            continue
        if next_index is None:
            add_way_metrics(kept[kept_position[previous_index]], way)
            continue

        previous = kept[kept_position[previous_index]]
        following = kept[kept_position[next_index]]

        if previous.terrain == following.terrain:
            add_way_metrics(previous, way, 0.5)
            add_way_metrics(following, way, 0.5)
        elif terrain_distance(way.terrain, previous.terrain) <= terrain_distance(
            way.terrain, following.terrain
        ):
            add_way_metrics(previous, way)
        else:
            add_way_metrics(following, way)

    return kept


def town_visit_key(way):
    town = way.town or {}
    return town.get("town_code"), town.get("name")


def absorb_short_ways(ways_info, minimum=MIN_ROAD_BOOK_SEGMENT_METERS):
    """
    Retire les segments courts et reporte leurs métriques sur le segment
    conservé le plus proche en type de surface. Si les surfaces précédente
    et suivante sont identiques, la contribution est partagée à parts égales.
    Le traitement reste strictement limité à chaque traversée de ville et
    conserve toujours au moins une voie par étape.
    """
    if not ways_info:
        return []

    result = []
    block = []
    current_key = None
    for way in ways_info:
        key = town_visit_key(way)
        if block and key != current_key:
            result.extend(absorb_short_town_block(block, minimum))
            block = [way]
        else:
            block.append(way)
        current_key = key

    result.extend(absorb_short_town_block(block, minimum))
    return result


def compress_ways(ways_info):
    compressed = []
    for way in ways_info:
        if re.fullmatch(r"Track\d+", way.terrain):
            way.terrain = "Track"
        if compressed and way.title == compressed[-1].title and way.terrain == compressed[-1].terrain:
            compressed[-1].distance += way.distance
            compressed[-1].elevationPositif += way.elevationPositif
            compressed[-1].elevationNegatif += way.elevationNegatif
        else:
            compressed.append(way)
    return compressed


def upgrade_ways(ways_info):
    osmids = []
    for way in ways_info:
        if o.is_osmid_positive(way.way["osmid"]):
            if isinstance(way.way["osmid"], int):
                osmids.append(way.way["osmid"])
            else:
                osmids.extend(way.way["osmid"])

    if osmids:
        query = f"""[out:json];way(id:{','.join(map(str, osmids))});out tags;"""
        ways_data = o.overpass(query)
        ways_data_dict = {item["id"]: item["tags"] for item in ways_data}
    else:
        ways_data_dict = {}

    for way in ways_info:
        osmid = way.way["osmid"]
        if o.is_osmid_positive(osmid):
            lookup_id = osmid if isinstance(osmid, int) else osmid[0]
            if lookup_id in ways_data_dict:
                way.update_tags(ways_data_dict[lookup_id])
        way.update_title()


def format_plus(ways_info):
    visit_slopes = {}
    block_start = 0
    while block_start < len(ways_info):
        key = town_visit_key(ways_info[block_start])
        block_end = block_start
        block_distance = 0
        block_gain = 0
        while block_end < len(ways_info) and town_visit_key(ways_info[block_end]) == key:
            block_distance += ways_info[block_end].distance
            block_gain += max(0, ways_info[block_end].elevationPositif)
            block_end += 1
        visit_slopes[block_start] = 100 * block_gain / block_distance if block_distance else 0
        block_start = block_end

    lines = []
    distance = 0
    elevation_gain = 0
    town_name = None
    town_number = 0

    for i, way in enumerate(ways_info):
        if town_name is None or (way.town is not None and town_name != way.town["name"]):
            town_number += 1
            insee = way.town.get("town_code") or ""
            town = way.town["name"]
            web = way.town.get("web")
            town_md = f"[{town}]({web})" if isinstance(web, str) and web else town
            route_km = f"km{o.meter_2_km(distance)}"
            cumulative_gain = f"{round(elevation_gain)}d+"
            slope = f"{math.floor(visit_slopes[i] + 0.5)}%"
            lines.append(
                f"\n{town_number} - {route_km} - {cumulative_gain} - {slope} - "
                f"{insee} {town_md}\n"
            )
            town_name = town

        distance += way.distance
        elevation_gain += max(0, way.elevationPositif)
        lines.append(
            f"{way.title} ({way.terrain}) {o.distance_lisible(way.distance)} "
            f"+{round(way.elevationPositif)}/{round(way.elevationNegatif)}\n"
        )

    lines.append(f"\n{o.distance_lisible(distance)}")
    return "".join(lines)


def export_route_towns(towns, path):
    """Sauvegarde une source locale et internationale des communes du parcours."""
    fields = (
        "name",
        "code",
        "postal_code",
        "population",
        "place",
        "capital",
        "administrative_status",
        "designation",
        "website",
        "wikipedia",
        "wikidata",
    )
    with open(path, "w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for name, town in towns.towns.items():
            writer.writerow(
                {
                    "name": name,
                    "code": town.get("code_ville", ""),
                    "postal_code": town.get("code_postal", ""),
                    "population": town.get("population", ""),
                    "place": town.get("place", ""),
                    "capital": town.get("capital", ""),
                    "administrative_status": " | ".join(
                        town.get("administrative_status", [])
                    ),
                    "designation": town.get("designation", ""),
                    "website": town.get("web", ""),
                    "wikipedia": town.get("wikipedia", ""),
                    "wikidata": town.get("wikidata", ""),
                }
            )


def osm_administrative_statuses(frame):
    """Statuts des centres administratifs OSM rencontrés dans l'emprise."""
    bbox = (
        frame["min_lat"],
        frame["min_lon"],
        frame["max_lat"],
        frame["max_lon"],
    )
    query = f"""[out:json];
relation["boundary"="administrative"]["admin_level"~"^(4|6|7)$"]{bbox}->.rels;
(.rels;node(r.rels:"admin_centre"););out body;
"""
    elements = o.overpass(query)
    nodes = {
        element["id"]: element.get("tags", {})
        for element in elements
        if element.get("type") == "node"
    }
    statuses = defaultdict(list)

    for relation in elements:
        if relation.get("type") != "relation":
            continue
        tags = relation.get("tags", {})
        admin_level = str(tags.get("admin_level", ""))
        iso_code = str(tags.get("ISO3166-2", "")).upper()

        if admin_level == "4":
            label = "Capitale régionale"
        elif admin_level == "6":
            label = "Préfecture" if iso_code.startswith("FR-") else "Capitale provinciale"
        elif admin_level == "7":
            label = "Sous-préfecture" if iso_code.startswith("FR-") else "Centre administratif"
        else:
            continue

        for member in relation.get("members", []):
            if member.get("type") != "node" or member.get("role") != "admin_centre":
                continue
            node = nodes.get(member.get("ref"), {})
            name = node.get("name")
            if name and label not in statuses[normalize_name(name)]:
                statuses[normalize_name(name)].append(label)

    return statuses


def generate_raw_books(directory, towns_csv_path):
    """Calcule les communes et voies puis écrit les deux Markdown techniques."""
    cache.init_cache(gpx_file)

    gpx = t.gpx_reader(gpx_path)
    title = t.gpx_name(gpx)
    meters = t.gpx_meters(gpx)
    frame = o.gpx_frame(gpx)

    towns = o.TownManager()
    towns.cities(frame)
    towns.gpx_villes(gpx, meters)
    administrative_statuses = osm_administrative_statuses(frame)

    OSM_TOWN_TAGS.clear()
    for name, town in towns.towns.items():
        insee = town.get("code_ville")
        town_statuses = list(administrative_statuses.get(normalize_name(name), []))
        if insee:
            town_statuses = [
                "Sous-préfecture" if status == "Centre administratif" else status
                for status in town_statuses
            ]
        elif "Centre administratif" in town_statuses and len(town_statuses) > 1:
            town_statuses.remove("Centre administratif")
        tags = {
            "population": town.get("population", ""),
            "place": town.get("place", ""),
            "capital": town.get("capital", ""),
            "administrative_status": town_statuses,
            "designation": town.get("designation", ""),
            "website": town.get("web", ""),
            "wikipedia": town.get("wikipedia", ""),
            "wikidata": town.get("wikidata", ""),
        }
        if insee:
            OSM_TOWN_TAGS[str(insee)] = tags
        OSM_TOWN_TAGS[normalize_name(name)] = tags
        town["administrative_status"] = tags["administrative_status"]

    export_route_towns(towns, towns_csv_path)

    road_book = os.path.join(directory, "road_book.md")
    with open(road_book, "w", encoding="utf-8") as output:
        output.write(f"# {title}\n\n")
        output.write(f"{towns.towns_numering()} communes\n\n")
        output.write(towns.vformat())

    ways = o.Ways(output_folder)
    ways.gpx_2_polygons(gpx, meters)
    ways.polygons_ways()
    elevations = t.gpx_elevations(gpx)
    ways_info = []
    total_points = sum(len(segment.points) for track in gpx.tracks for segment in track.segments)

    with tqdm(total=total_points, desc="Ways info") as progress:
        point_index = 0
        for track in gpx.tracks:
            for segment in track.segments:
                for index, point in enumerate(segment.points):
                    if index == 0:
                        point_index += 1
                        continue
                    previous = segment.points[index - 1]
                    route_segment = (
                        (previous.latitude, previous.longitude),
                        (point.latitude, point.longitude),
                    )
                    distance = meters[point_index] - meters[point_index - 1]
                    elevation = elevations[point_index] - elevations[point_index - 1]
                    town = towns.locate_point_in_town(point.latitude, point.longitude)
                    way = ways.locate_way(point.latitude, point.longitude)
                    ways_info.append(o.Infos(distance, elevation, town, way, route_segment))
                    point_index += 1
                    progress.update(1)

    upgrade_ways(ways_info)
    road_book_plus = os.path.join(directory, "road_book_plus.md")
    with open(road_book_plus, "w", encoding="utf-8") as output:
        output.write(f"# {title}\n\n")
        output.write(format_plus(compress_ways(absorb_short_ways(ways_info))))

    cache.close_cache()
    return road_book, road_book_plus


def output_paths():
    stem = gpx_file.removesuffix(".gpx")
    return {
        "road_book": os.path.join(output_folder, f"{stem}_road_book.md"),
        "road_book_plus": os.path.join(output_folder, f"{stem}_road_book_plus.md"),
        "towns_csv": os.path.join(output_folder, f"{stem}_towns.csv"),
    }


# ----------------------------------------------------------------------------
# Traitement générique (road_book et road_book_plus)
# ----------------------------------------------------------------------------

def resolve_group(name, insee, pool_cache, ambiguous_positions, anchors, gpos):
    """Détermine le pool de candidats pour une visite : direct via INSEE
    (zéro ambiguïté) si possible, sinon repli par nom (avec ancrage)."""
    if insee and insee in INSEE_INDEX:
        candidate = csv_row_to_candidate(INSEE_INDEX[insee])
        return [candidate], True  # (pool, résolu_directement)

    return get_pool_by_name_fallback(name), False


def process_road_book_generic(input_path, output_path, cache_namespace):
    with open(input_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    records = [parse_town_line(raw) for raw in lines]
    town_indices = [i for i, r in enumerate(records) if r is not None]

    # Regroupe les lignes consécutives concernant la même commune (la trace
    # peut repasser plusieurs fois par le même village).
    groups = []
    for idx in town_indices:
        name = records[idx]["name"]
        insee = records[idx]["insee"]
        if groups and groups[-1]["name"] == name:
            groups[-1]["indices"].append(idx)
        else:
            groups.append({"name": name, "insee": insee, "indices": [idx]})

    # ------------------------------------------------------------------
    # Passe 1 : résolution directe par code INSEE (cas normal, zéro
    # ambiguïté) ; sinon repli par nom + repérage des ancres géographiques
    # fiables pour les rares cas qui doivent encore être désambiguïsés.
    # ------------------------------------------------------------------
    pools, anchors, ambiguous_positions, direct_hits = {}, {}, [], 0

    for gpos, group in enumerate(groups):
        name, insee = group["name"], group["insee"]

        if insee and insee in INSEE_INDEX:
            pools[gpos] = [csv_row_to_candidate(INSEE_INDEX[insee])]
            direct_hits += 1
            continue

        osm_candidate = osm_candidate_by_name(name)
        if osm_candidate:
            print(f"Pas de code INSEE pour '{name}' -> données OSM du parcours")
            pool = [osm_candidate]
        else:
            print(f"Pas de code INSEE exploitable pour '{name}' -> repli recherche par nom")
            pool = get_pool_by_name_fallback(name)
        pools[gpos] = pool

        coords_list = [c["coords"] for c in pool if c.get("coords")]
        if not coords_list:
            ambiguous_positions.append(gpos)
            continue
        if len(coords_list) == 1:
            anchors[gpos] = coords_list[0]
            continue
        spread = max(haversine_km(coords_list[0], c) for c in coords_list[1:])
        if spread <= ANCHOR_SPREAD_KM:
            anchors[gpos] = coords_list[0]
        else:
            ambiguous_positions.append(gpos)

    # ------------------------------------------------------------------
    # Passe 2 : choix du candidat pour chaque visite (direct par INSEE,
    # ou ancre, ou homonyme résolu via l'ancre fiable la plus proche
    # avant/après sur la trace).
    # ------------------------------------------------------------------
    chosen = {}

    for gpos, pool in pools.items():
        if len(pool) == 1:
            chosen[gpos] = pool[0]

    for gpos in anchors:
        if gpos in chosen:
            continue
        pool = pools[gpos]
        target = anchors[gpos]
        chosen[gpos] = min(
            pool,
            key=lambda c: (haversine_km(target, c["coords"]) if c.get("coords") else float("inf")),
        )

    for gpos in ambiguous_positions:
        name = groups[gpos]["name"]
        before_coords, after_coords = None, None
        for p in range(gpos - 1, -1, -1):
            if p in anchors:
                before_coords = anchors[p]
                break
        for p in range(gpos + 1, len(groups)):
            if p in anchors:
                after_coords = anchors[p]
                break

        pool = pools[gpos]
        scored = []
        for cand in pool:
            coords = cand.get("coords")
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
                scored.append((dist, cand))

        if scored:
            scored.sort(key=lambda x: x[0])
            chosen[gpos] = scored[0][1]
            print(f"  Homonymes pour '{name}' -> choix par proximité géographique")
        else:
            chosen[gpos] = pool[0] if pool else None
            if len(pool) > 1:
                print(
                    f"  ! '{name}' : {len(pool)} homonymes sans coordonnées "
                    "utilisables, choix arbitraire (à vérifier)."
                )

    # ------------------------------------------------------------------
    # Passe 3 : résolution finale (cache) + écriture.
    # ------------------------------------------------------------------
    new_lines = list(lines)
    stats = {"official": 0, "wikipedia": 0, "missing": [], "direct_insee": direct_hits}

    for gpos, group in enumerate(groups):
        name = group["name"]
        candidate = chosen.get(gpos)

        hash_key = cache.create_hash(name, f"{cache_namespace}::pos{gpos}")
        found, cached = cache.get_cache(hash_key)
        if found:
            official_url, wiki_url = cached
        else:
            official_url, wiki_url = resolve_town_links(candidate)
            cache.into_cache(hash_key, (official_url, wiki_url))

        metadata = town_metadata(candidate)

        if official_url:
            stats["official"] += 1
            print(f"{name} -> officiel : {official_url}")
        elif wiki_url:
            stats["wikipedia"] += 1
            print(f"{name} -> wikipedia : {wiki_url}")
        else:
            stats["missing"].append(name)
            print(f"{name} -> rien trouvé")

        for idx in group["indices"]:
            new_lines[idx] = rebuild_line(records[idx], official_url, wiki_url, metadata)

    with open(output_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    print(f"\n---- Résumé ({os.path.basename(input_path)}) ----")
    print(f"Résolues directement par code INSEE : {stats['direct_insee']}/{len(groups)}")
    print(f"Sites officiels trouvés  : {stats['official']}")
    print(f"Pages Wikipédia trouvées : {stats['wikipedia']}")
    print(f"Communes sans aucun lien : {len(stats['missing'])}")
    if stats["missing"]:
        print("Communes à vérifier manuellement :")
        for m in stats["missing"]:
            print(f"  - {m}")
    print(f"Fichier généré : {output_path}\n")

    return output_path


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    paths = output_paths()
    with tempfile.TemporaryDirectory(prefix="img2gpx-books-") as temporary_directory:
        raw_book, raw_book_plus = generate_raw_books(temporary_directory, paths["towns_csv"])
        cache.init_cache("villes_links")
        process_road_book_generic(
            raw_book,
            paths["road_book"],
            cache_namespace="wikidata_resolve_rb_v7_insee",
        )
        process_road_book_generic(
            raw_book_plus,
            paths["road_book_plus"],
            cache_namespace="wikidata_resolve_rbplus_v4_insee",
        )

    cache.close_cache()
