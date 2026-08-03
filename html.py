"""
Génère les versions HTML des road books Markdown existants.

Dans le road_book_plus, une ou plusieurs lignes commençant par "> " sous les
voies d'une ville sont rendues comme commentaire manuel en pied de cette ville.

Usage :
    python3 html.py
"""

import os
import re
import sysconfig

# Puisque ce fichier porte le même nom que le paquet standard, permettre aux
# bibliothèques tierces de continuer à importer notamment ``html.entities``.
__path__ = [os.path.join(sysconfig.get_path("stdlib"), "html")]

from parameters import gpx_file, output_folder


INSEE_RE = r"\d{5}|2[AB]\d{3}"
TITLE_RE = re.compile(r"^#\s+(.+)$")
COUNT_RE = re.compile(r"^(\d+\s+communes)\s*$")
STANDARD_RE = re.compile(
    r"^(?P<km>km\d+)\s*-\s*(?:(?P<insee>" + INSEE_RE + r")\s+)?"
    r"(?P<body>.+?)\s+(?P<distance>[\d.]+km)$"
)
PLUS_RE = re.compile(
    r"^(?P<number>\d+)\s*-\s*(?:(?P<route_km>km\d+)\s*-\s*)?"
    r"(?:(?P<gain>\d+d\+)\s*-\s*)?"
    r"(?:(?P<slope>[\d.]+%)\s*-\s*)?"
    r"(?:(?P<insee>" + INSEE_RE + r")\s+)?(?P<body>.+)$"
)
LINK_RE = re.compile(r"^\[(?P<name>.+?)\]\((?P<url>.*?)\)$")
WIKI_RE = re.compile(r"\s*\[\(w\)\]\((?P<url>[^)]*)\)\s*$")
INLINE_LINK_RE = re.compile(r"\[(?P<label>[^\]]+)\]\((?P<url>[^)]+)\)")
WAY_RE = re.compile(
    r"^(?P<name>.+?)\s+\((?P<terrain>[^)]+)\)\s+"
    r"(?P<distance>[\d.]+(?:km|m))\s+(?P<elevation>\+\S+/\S+)$"
)
METADATA_RE = re.compile(r"^\*(?P<text>.+)\*$")


# Le nom de ce script masque le module standard "html" lorsqu'il est importé
# depuis le projet. Exposer escape maintient la compatibilité attendue.
def escape(value, quote=True):
    text = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if quote:
        text = text.replace('"', "&quot;").replace("'", "&#x27;")
    return text


def split_town_body(body):
    body = body.strip()
    wiki_url = None
    wiki = WIKI_RE.search(body)
    if wiki:
        wiki_url = wiki.group("url") or None
        body = body[: wiki.start()].rstrip()

    link = LINK_RE.match(body)
    if link:
        return link.group("name"), link.group("url") or None, wiki_url
    return body, None, wiki_url


def parse_town(line, plus=False):
    match = (PLUS_RE if plus else STANDARD_RE).match(line.strip())
    if not match:
        return None
    values = match.groupdict()
    name, url, wiki_url = split_town_body(values.pop("body"))
    values.update(name=name, url=url, wiki_url=wiki_url)
    return values


def town_link(record):
    name = escape(record["name"])
    if record["url"]:
        name = (
            f'<a href="{escape(record["url"])}" target="_blank" rel="noopener">'
            f"{name}</a>"
        )
    if record["wiki_url"]:
        name += (
            f' <a class="wiki" href="{escape(record["wiki_url"])}" '
            f'target="_blank" rel="noopener">(w)</a>'
        )
    return name


def inline_markdown(text):
    """Échappe le commentaire tout en convertissant ses liens Markdown."""
    rendered = []
    position = 0
    for match in INLINE_LINK_RE.finditer(text):
        rendered.append(escape(text[position : match.start()]))
        rendered.append(
            f'<a href="{escape(match.group("url"))}" target="_blank" rel="noopener">'
            f'{escape(match.group("label"))}</a>'
        )
        position = match.end()
    rendered.append(escape(text[position:]))
    return "".join(rendered)


def page(title, content, extra_css="", footer="Généré automatiquement."):
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
  background: #f7f5f0; color: #222; margin: 0; padding: 2rem 1rem; }}
main {{ max-width: 900px; margin: 0 auto; }}
h1 {{ font-size: 1.7rem; margin: 0 0 1.5rem; }}
a {{ color: #1a5fb4; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
a.wiki {{ color: #888; font-size: .85em; }}
footer {{ color: #999; font-size: .8rem; text-align: center; margin-top: 2rem; }}
{extra_css}
</style>
</head>
<body><main><h1>{escape(title)}</h1>{content}<footer>{escape(footer)}</footer></main></body>
</html>
"""


def generate_standard(markdown_path, html_path):
    title = "Road book"
    count = ""
    entries = []
    current = None
    with open(markdown_path, encoding="utf-8") as source:
        for line in source:
            stripped = line.strip()
            title_match = TITLE_RE.match(stripped)
            if title_match:
                title = title_match.group(1).strip()
                continue
            count_match = COUNT_RE.match(stripped)
            if count_match:
                count = count_match.group(1)
                continue
            record = parse_town(line)
            if record:
                record["metadata"] = ""
                entries.append(record)
                current = record
                continue
            metadata = METADATA_RE.match(stripped)
            if metadata and current is not None:
                current["metadata"] = metadata.group("text")

    rows = "".join(
        "<li>"
        f'<span class="km">{escape(item["km"])}</span>'
        f'<span class="town">{town_link(item)}</span>'
        f'<span class="metadata">{escape(item.get("metadata", ""))}</span>'
        f'<span class="distance">{escape(item["distance"])}</span>'
        "</li>"
        for item in entries
    )
    content = f'<p class="count">{escape(count)}</p><ul>{rows}</ul>'
    css = """
.count { color: #666; }
ul { list-style: none; padding: 0; border-top: 1px solid #ddd; }
li { display: flex; gap: .75rem; padding: .5rem 0; border-bottom: 1px solid #eee; }
.km { font-weight: 600; color: #a33; min-width: 4.5rem; }
.town { flex: 1; }
.metadata { color: #777; font-size: .85rem; }
.distance { color: #888; white-space: nowrap; }
"""
    with open(html_path, "w", encoding="utf-8") as output:
        output.write(page(title, content, css))


def generate_plus(markdown_path, html_path):
    title = "Road book détaillé"
    sections = []
    current = None
    total = ""

    with open(markdown_path, encoding="utf-8") as source:
        for line in source:
            stripped = line.strip()
            title_match = TITLE_RE.match(stripped)
            if title_match:
                title = title_match.group(1).strip()
                continue

            town = parse_town(line, plus=True)
            if town:
                current = {"town": town, "ways": [], "comments": []}
                sections.append(current)
                continue

            way = WAY_RE.match(stripped)
            if way and current is not None:
                current["ways"].append(way.groupdict())
            elif METADATA_RE.match(stripped) and current is not None:
                current["town"]["metadata"] = METADATA_RE.match(stripped).group("text")
            elif re.fullmatch(r"[\d.]+(?:km|m)", stripped):
                total = stripped
            elif stripped and current is not None:
                # Un paragraphe Markdown libre placé après les voies appartient
                # à la ville courante. Le préfixe ">" reste accepté mais n'est
                # plus obligatoire.
                comment = stripped[1:].strip() if stripped.startswith(">") else stripped
                if comment:
                    current["comments"].append(comment)

    cards = []
    for section in sections:
        town = section["town"]
        metadata = " · ".join(
            value
            for value in (town.get("route_km"), town.get("gain"), town.get("slope"))
            if value
        )
        town_details = town.get("metadata", "")
        ways = "".join(
            "<li>"
            f'<span class="way-name">{escape(way["name"])}</span>'
            f'<span class="terrain">{escape(way["terrain"])}</span>'
            f'<span class="distance">{escape(way["distance"])}</span>'
            f'<span class="elevation">{escape(way["elevation"])}</span>'
            "</li>"
            for way in section["ways"]
        )
        comments = "".join(f"<p>{inline_markdown(comment)}</p>" for comment in section["comments"])
        comment_footer = f'<div class="comment">{comments}</div>' if comments else ""
        cards.append(
            '<section class="stop"><div class="heading">'
            f'<span class="number">{escape(town["number"])}</span><div>'
            f"<h2>{town_link(town)}</h2><p class=\"metadata\">{escape(metadata)}</p>"
            f'<p class="town-details">{escape(town_details)}</p>'
            f'</div></div><ul class="ways">{ways}</ul>{comment_footer}</section>'
        )

    content = "".join(cards) + f'<p class="total">Distance totale : {escape(total)}</p>'
    css = """
.stop { background: #fff; border: 1px solid #e1ddd4; border-radius: 10px;
  margin-bottom: 1rem; overflow: hidden; }
.heading { display: flex; gap: .8rem; align-items: center; padding: .8rem 1rem; }
.number { display: grid; place-items: center; min-width: 2rem; height: 2rem;
  border-radius: 50%; background: #a33; color: #fff; font-weight: 700; }
h2 { font-size: 1.1rem; margin: 0; }
.metadata { margin: .15rem 0 0; color: #777; font-size: .9rem; }
.town-details { margin: .15rem 0 0; color: #555; font-size: .85rem; }
.ways { list-style: none; padding: 0; margin: 0; border-top: 1px solid #eee; }
.ways li { display: grid; grid-template-columns: minmax(12rem,1fr) 10rem 5rem 5rem;
  gap: .7rem; padding: .42rem 1rem; border-bottom: 1px solid #f0f0f0; }
.terrain,.distance,.elevation { color: #777; }
.distance,.elevation { text-align: right; white-space: nowrap; }
.comment { padding: .65rem 1rem; background: #fff8df; border-top: 1px solid #eadca9;
  color: #5c5030; font-style: italic; }
.comment p { margin: .2rem 0; }
.total { text-align: right; font-weight: 700; margin: 1.5rem 0; }
@media (max-width:650px) {
  .ways li { grid-template-columns: 1fr auto auto; }
  .terrain { grid-column: 1/-1; font-size: .85rem; }
}
"""
    with open(html_path, "w", encoding="utf-8") as output:
        output.write(page(title, content, css))


def paths():
    stem = gpx_file.removesuffix(".gpx")
    return {
        "markdown": os.path.join(output_folder, f"{stem}_road_book.md"),
        "markdown_plus": os.path.join(output_folder, f"{stem}_road_book_plus.md"),
        "html": os.path.join(output_folder, f"{stem}_road_book.html"),
        "html_plus": os.path.join(output_folder, f"{stem}_road_book_plus.html"),
    }


if __name__ == "__main__":
    files = paths()
    generate_standard(files["markdown"], files["html"])
    generate_plus(files["markdown_plus"], files["html_plus"])
    print(f"Pages HTML générées : {files['html']} et {files['html_plus']}")
