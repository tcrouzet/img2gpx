from parameters import assets_dir, output_folder, logs_file, gpx_file, gpx_path
import tools as t
from network_check import NetworkDiagnostic
import osm_tools as o
import os, sys
from tqdm import tqdm
import cache_manager as cache
import warnings

os.system('clear')

sys.stdout = t.DualOutput(logs_file)
sys.stderr = sys.stdout

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


def upgrade_ways(ways_info):

    osmids =[]
    for way in ways_info:
        if o.is_osmid_positive(way.way['osmid']):
            if isinstance(way.way['osmid'], int):
                osmids.append(way.way['osmid'])
            else:
                osmids.extend(way.way['osmid'])

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


diag = NetworkDiagnostic(suspect_host="z.overpass-api.de")
result = diag.run()

t.pd("Acquisition towns started")

gpx = t.gpx_reader(gpx_path)
gpx_name = t.gpx_name(gpx)
t.pd(gpx_name)
meters = t.gpx_meters(gpx)
t.pd("Meters done")
frame = o.gpx_frame(gpx)

villes_info = o.TownManager()
villes_info.cities(frame)
t.pd("Acquisition towns ended")
#t.save_gdf(villes_info.communes_gdf, os.path.join(output_folder,"towns.txt"))

villes_info.gpx_villes(gpx, meters)
t.pd("GPX ville ended")

#print(villes_info.get_postal_codes())
road_html =  os.path.join(output_folder, gpx_file.replace(".gpx",".html"))
traversed = villes_info.get_traversed_communes_gdf()
#traversed = villes_info.geometry_by_code(34277)
#o.folium_minimal(road_html, traversed, gpx)
#o.plot_communes(road_png, traversed, villes_info, gpx, gpx_name)
t.pd("Plot communes OK")

t.pd("Acquisition ways started")
voies = o.Ways(output_folder)
voies.gpx_2_polygons(gpx,meters)
#o.show_polygons(voies.polygon_frames)
t.pd("Polygons done")
voies.polygons_ways()
t.pd("Ways done")
#voies.plot_graph(voies.ways_graph)
#G = osmnx.graph_from_place('Piedmont, California, USA', network_type='drive')
#polygons = voies.graph_to_polygons(G)
#o.show_polygons( polygons )

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
            town = villes_info.locate_point_in_town(point.latitude, point.longitude)
            way = voies.locate_way(point.latitude, point.longitude)
            ways_info.append( o.Infos(distance_way, elevation_way, town, way, segment) )

            pbar.update(1)

pbar.close()
t.pd("Localisation ways ended")
            
upgrade_ways(ways_info)
t.pd("Upgrade ways ended")

road_html =  os.path.join(output_folder, gpx_file.replace(".gpx",".html"))
o.plot_communes_folium(road_html, traversed, ways_info, gpx_name)

# Map avec link OSM
# road_html = os.path.join(output_folder, gpx_file.replace(".gpx","_link.html"))
# o.folium_ways2(road_html, traversed, ways_info, gpx_name)
