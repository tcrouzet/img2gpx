"""
citieslink.py
--------------
Lit les road books markdown générés par gpxcities.py / osm_tools.py :
  - "<nom_gpx>_road_book.md"       (format "kmXXX - INSEE Nom Xkm")
  - "<nom_gpx>_road_book_plus.md"  (format "N - INSEE [Nom](url)" + détail des voies)

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

Génère aussi une page HTML autonome (pas d'images, CSS inline) directement
publiable, à partir du road_book (avec kilométrage) :
output_folder/<gpx_file>_road_book.html.

Usage:
    python citieslink.py
"""

import os
import re
import csv
import time
import html
import math
import unicodedata
import urllib.parse
from collections import defaultdict

import requests

from parameters import output_folder, gpx_file, assets_dir
import cache_manager as cache

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
HEADERS = {"User-Agent": "TourmagneLinkFinder/1.0 (contact: tc@tcrouzet.com)"}
REQUEST_PAUSE = 0.3  # secondes entre deux requêtes réseau
ANCHOR_SPREAD_KM = 5.0  # candidats considérés "au même endroit" si <5km (filet de sécurité)

INSEE_RE = r"\d{5}|2[AB]\d{3}"

# Format "road_book"      : "kmXXX - [INSEE] <body> Xkm"
LINE_RE = re.compile(
    r"^(?P<km>km\d+)\s*-\s*(?:(?P<insee>" + INSEE_RE + r")\s+)?(?P<body>.+?)\s+(?P<dist>[\d.]+km)\s*$"
)
# Format "road_book_plus" : "N - [INSEE] Nom" ou "N - [INSEE] [Nom](url)"
# (gpxcities.py n'entoure de crochets que s'il y a réellement une URL,
# pour rester cohérent avec le road_book classique).
PLUS_LINE_RE = re.compile(
    r"^(?P<km>\d+)\s*-\s*(?:(?P<insee>" + INSEE_RE + r")\s+)?(?P<body>.+)$"
)
# Un nom déjà lié : "[Nom](url)"
LINK_RE = re.compile(r"^\[(?P<name>.+?)\]\((?P<url>.*?)\)$")
# Badge wikipédia déjà présent en fin de ligne : "... [(w)](url)"
WIKI_BADGE_RE = re.compile(r"\s*\[\(w\)\]\((?P<wiki>[^)]*)\)\s*$")

TITLE_RE = re.compile(r"^#\s+(.+)$")
COUNT_RE = re.compile(r"^(\d+\s+communes)\s*$")

cache.init_cache("villes_links")


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
            "km": m.group("km"),
            "insee": m.group("insee"),
            "name": name,
            "url": url,
            "wiki_url": wiki_url,
        }

    return None


def rebuild_line(record, official_url, wiki_url):
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
        return f"{record['km']} - {town_md}\n"
    return f"{record['km']} - {town_md} {record['dist']}\n"


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
# Localisation des fichiers
# ----------------------------------------------------------------------------

def find_road_books():
    road_book = os.path.join(output_folder, gpx_file.replace(".gpx", "_road_book.md"))
    road_book_plus = os.path.join(output_folder, gpx_file.replace(".gpx", "_road_book_plus.md"))
    road_book_links = os.path.join(output_folder, gpx_file.replace(".gpx", "_road_book_links.md"))
    road_book_plus_links = os.path.join(output_folder, gpx_file.replace(".gpx", "_road_book_plus_links.md"))
    html = os.path.join(output_folder, gpx_file.replace(".gpx", "_road_book.html"))

    if os.path.exists(road_book) and os.path.exists(road_book_plus):
        return {
            "road_book": road_book,
            "road_book_plus": road_book_plus,
            "road_book_links": road_book_links,
            "road_book_plus_links": road_book_plus_links,
            "html": html
        }

    raise FileNotFoundError(
        f"{road_book} et/ou {road_book_plus} introuvables. "
        "Générez d'abord les road books avec gpxcities.py."
    )


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

        hash_key = cache.create_hash(name, f"{cache_namespace}::pos{gpos}")
        found, cached = cache.get_cache(hash_key)
        if found:
            official_url, wiki_url = cached
        else:
            official_url, wiki_url = resolve_town_links(chosen.get(gpos))
            cache.into_cache(hash_key, (official_url, wiki_url))

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
            new_lines[idx] = rebuild_line(records[idx], official_url, wiki_url)

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
# Page HTML autonome (à partir du road_book, qui porte le kilométrage)
# ----------------------------------------------------------------------------

def parse_full_md(path):
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

        record = parse_town_line(line)
        if record and record["format"] == "road_book":
            entries.append(record)

    return title, count_line, entries


def generate_html(md_path, html_path):
    title, count_line, entries = parse_full_md(md_path)

    rows = []
    for record in entries:
        safe_name = html.escape(record["name"])

        if record["url"]:
            town_html = (
                f'<a href="{html.escape(record["url"])}" target="_blank" rel="noopener">'
                f"{safe_name}</a>"
            )
        else:
            town_html = safe_name

        if record["wiki_url"]:
            town_html += (
                f' <a class="wiki" href="{html.escape(record["wiki_url"])}" '
                f'target="_blank" rel="noopener">(w)</a>'
            )

        rows.append(
            "<li>"
            f'<span class="km">{html.escape(record["km"])}</span>'
            f'<span class="town">{town_html}</span>'
            f'<span class="dist">{html.escape(record["dist"])}</span>'
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
  .town a.wiki {{ color: #888; font-size: 0.85em; }}
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
  <footer>Généré automatiquement — liens officiels ou Wikipédia via le CSV des communes / Wikidata.</footer>
</div>
</body>
</html>
"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(doc)

    print(f"Page HTML générée : {html_path}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    paths = find_road_books()

    rb_out = process_road_book_generic(
        paths["road_book"], paths["road_book_links"], cache_namespace="wikidata_resolve_rb_v7_insee"
    )
    process_road_book_generic(
        paths["road_book_plus"],
        paths["road_book_plus_links"],
        cache_namespace="wikidata_resolve_rbplus_v4_insee",
    )

    generate_html(rb_out, paths["html"])

    cache.close_cache()
