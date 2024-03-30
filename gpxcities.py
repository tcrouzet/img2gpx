from parameters import assets_dir, output_folder, logs_file
import tools as t
import osm_tools as o
import os, sys
from tqdm import tqdm
import cache_manager as cache
import warnings

os.system('clear')

sys.stdout = t.DualOutput(logs_file)
sys.stderr = sys.stdout

#gpx_file="route-balaruc.gpx"
#gpx_file="route-tourmagne.gpx"
gpx_file="route-727.gpx"

cache.init_cache(gpx_file)

original_showwarning = warnings.showwarning

def custom_showwarning(message, category, filename, lineno, file=None, line=None):
    # Ignorer uniquement les avertissements spécifiques de pyproj
    if "pyproj" in filename and "DeprecationWarning" in str(category).lower:
        return
    if "numpy" in filename and "DeprecationWarning" in str(category).lower:
        return
    
    # Pour tous les autres avertissements, utiliser le comportement par défaut
    original_showwarning(message, category, filename, lineno, file, line)

warnings.showwarning = custom_showwarning

def format_ligne(index, nom_ville, info):
        enter = o.meter_2_km(info['distance_enter'])
        lisible = o.distance_lisible(info['distance'])

        if info['web']:
            nom = nom_ville
        else:
            nom = f"[{nom_ville}]({info['web']})"

        return f"{index}. {nom} km{enter} {lisible}\n\n"


def get_traversed_communes_gdf(communes_gdf, villes_info):
    noms_de_villes = villes_info.get_town_names()
    codes_postaux = villes_info.get_postal_codes()

    traversed_communes = communes_gdf[
        (communes_gdf['name'].isin(noms_de_villes)) & 
        (communes_gdf['postal_code'].isin(codes_postaux))
    ]

    return traversed_communes


def compress_ways(ways_info):

    #Successifs
    new_ways = []
    for i, way in enumerate(ways_info):
        if i == 0:
            new_ways.append(way)
            continue
        if way.title == new_ways[-1].title and way.terrain == new_ways[-1].terrain:
            new_ways[-1].distance += way.distance
            new_ways[-1].elevationPositif += way.elevationPositif
            new_ways[-1].elevationNegatif += way.elevationNegatif
        else:
            new_ways.append(way)

    return new_ways

def sandwich_ways(new_ways):
    hyper_ways = []
    i = 0
    while i < len(new_ways):
        way = new_ways[i]
        if i == 0 or i == len(new_ways)-1:
            hyper_ways.append(way)
            i += 1
        elif o.is_osmid_positive(hyper_ways[-1].way['osmid']) and hyper_ways[-1].title == new_ways[i+1].title and compare_osmid(hyper_ways[-1].way['osmid'], new_ways[i+1].way['osmid']):
            #Sandwitch
            hyper_ways[-1].distance += way.distance + new_ways[i+1].distance
            i += 2
        else:
            hyper_ways.append(way)
            i += 1

    return hyper_ways


def format_ways(ways_info,index=None):
    md = ""
    #md = f"\n\nSegments: {len(ways_info)}"
    dist = 0
    town = None
    for i, way in enumerate(ways_info):
        if index and i==index:
            way.print_info()
            t.plot_graph(voies, way.segment)

        if town is None or town != way.town[0]:

            if isinstance(way.town[3], str):
                md += f"\n[{way.town[0]}]({way.town[3]})\n"
            else:
                md += f"\n{way.town[0]}\n"

            town = way.town[0]

        dist += way.distance
        #md += f"{i+1} {way.title} [{way.terrain}] {o.distance_lisible(way.distance)} {way.way['osmid']}\n"
        md += f"{way.title} [{way.terrain}] {o.distance_lisible(way.distance)} +{round(way.elevationPositif)}/{round(way.elevationNegatif)}\n"
    md += "\n"+o.distance_lisible(dist)
    return md


def compare_osmid(osmid1, osmid2):
    # Convertir les osmids en sets pour faciliter la comparaison, en s'assurant qu'ils sont sous forme de listes
    set1 = set([osmid1]) if isinstance(osmid1, int) else set(osmid1)
    set2 = set([osmid2]) if isinstance(osmid2, int) else set(osmid2)
    
    # Vérifier si les deux sets ont au moins un élément en commun
    return not set1.isdisjoint(set2)

    
def upgrade_ways(ways_info):

    osmids =[]
    for way in ways_info:
        if o.is_osmid_positive(way.way['osmid']):
            if isinstance(way.way['osmid'], int):
                osmids.append(way.way['osmid'])
            else:
                osmids.extend(way.way['osmid'])

    #print(osmids)
    #exit()

    query = f"""[out:json];way(id:{','.join(map(str, osmids))});out tags;"""
    ways_data = o.overpass(query)
    ways_data_dict = {item["id"]: item["tags"] for item in ways_data}

    for way in ways_info:
        if o.is_osmid_positive(way.way['osmid']):
            if isinstance(way.way['osmid'], int):
                if way.way["osmid"] in ways_data_dict:
                    way.update_tags(ways_data_dict[way.way["osmid"]])
            elif way.way["osmid"][0] in ways_data_dict:
                way.update_tags(ways_data_dict[way.way["osmid"][0]])
        way.update_title()


def export_book(path,ways_info):
    with open(path, 'w', encoding='utf-8') as fichier_md:
        fichier_md.write(format_ways(ways_info))


t.pd("Acquisition towns started")
gpx_path = os.path.join(assets_dir, gpx_file)
gpx = t.gpx_reader(gpx_path)
gpx_name = t.gpx_name(gpx)
t.pd(gpx_name)
meters = t.gpx_meters(gpx)
t.pd("Meters done")
frame = o.gpx_frame(gpx)
towns_gdf = o.cities(frame)
#print(towns.columns)
t.pd("Acquisition towns ended")

villes_info = o.TownManager()
o.gpx_villes(gpx,meters, towns_gdf, villes_info)
#villes_info.print()
t.pd("GPX ville ended")

road_book =  os.path.join(output_folder, gpx_file.replace(".gpx","_road_book.md"))
road_png =  os.path.join(output_folder, gpx_file.replace(".gpx","_road_book.png"))

with open(road_book, 'w', encoding='utf-8') as fichier_md:

    fichier_md.write( str(villes_info.towns_numering()) + " communes\n\n" )

    for index, nom_ville, infos in villes_info:

        fichier_md.write( format_ligne(index+1, nom_ville, infos) )

traversed_communes_gdf = get_traversed_communes_gdf(towns_gdf, villes_info)
o.plot_communes(road_png, traversed_communes_gdf, villes_info, gpx, gpx_name)
t.pd("Plot communes OK")

t.pd("Acquisition ways started")
polygon_frames = o.gpx_polygons(gpx,meters) 
#o.show_polygons(polygon_frames)
t.pd("Polygons done")

#voies = o.ways(frame)
voies = o.polygons_ways(polygon_frames)
#t.plot_graph(voies)
t.pd("Acquisition ways done")

elevations = t.gpx_elevations(gpx)
t.pd("Elevations done")

ways_info = []

total_points = sum(len(segment.points) for track in gpx.tracks for segment in track.segments)
pbar = tqdm(total=total_points, desc='Ways info:')

for track in gpx.tracks:
    for segment in track.segments:
        points = segment.points
        for i, point in enumerate(points):

            if i==0:
                continue

            segment=((points[i-1].latitude, points[i-1].longitude),(point.latitude, point.longitude))
            distance_way = meters[i]-meters[i-1]
            elevation_way = elevations[i] - elevations[i-1]
            town = o.locate_point_in_town(point.latitude, point.longitude, towns_gdf)

            way = o.locate_way(point.latitude, point.longitude, voies)
            #way = o.locate_way_path(segment, voies)
            ways_info.append(o.WayInfo(distance_way,elevation_way,town,way,segment))

            pbar.update(1)

pbar.close()
                 
t.pd("Localisation ways ended")
            
upgrade_ways(ways_info)

road_png =  os.path.join(output_folder, gpx_file.replace(".gpx","_road_book_plus.png"))
o.plot_communes(road_png, traversed_communes_gdf, villes_info, ways_info, gpx_name)
road_html =  os.path.join(output_folder, gpx_file.replace(".gpx","_road_book_plus.html"))
o.plot_communes_folium(road_html, traversed_communes_gdf, villes_info, ways_info, gpx_name)

road_book = os.path.join(output_folder, gpx_file.replace(".gpx","_road_book_plus.md"))

#export_book(ways_info)
ways_info = compress_ways(ways_info)
export_book(road_book, ways_info)

#ways_info = sandwich_ways(ways_info)
