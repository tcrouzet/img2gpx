from shapely.geometry import Point, LineString, Polygon, MultiLineString
from shapely.ops import linemerge
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import geopandas as gpd
import osmnx
import networkx as nx
import requests, math, os
import numpy as np
from tqdm import tqdm
import cache_manager as cache
from parameters import output_folder
import webbrowser, folium
from branca.element import MacroElement, Element, Figure
from itertools import groupby

from jinja2 import Template


class TownManager:
    def __init__(self):
        self.towns = {}

    def towns_numering(self):
        return len(self.towns)

    def update_town(self, nom_ville, code_postal, population, web=None, distance_enter=None, distance=None):
        if not nom_ville:
            exit("Nom de ville requis")

        if nom_ville in self.towns:
            if distance is not None:
                self.towns[nom_ville]['distance'] += distance
        else:
            if distance_enter is not None and distance is not None:
                self.towns[nom_ville] = {
                    'distance_enter': distance_enter,
                    'distance': distance,
                    'code_postal': code_postal.split(";")[0],
                    'population': population,
                    'web': web,
                    }
                
    def get_town_names(self):
        return list(self.towns.keys())

    def get_postal_codes(self):
        return [info['code_postal'] for info in self.towns.values()]
    
    def print(self):
        for index, (nom_ville, infos) in enumerate(self.towns.items()):
            enter = meter_2_km(infos['distance_enter'])
            lisible = distance_lisible(infos['distance'])

            print(f"{index}. {self.town_md(nom_ville)} km{enter} {lisible} {infos['code_postal']} {infos['population']} {infos['web']}\n")

    def town_md(self,nom_ville):
        
        if isinstance(self.towns[nom_ville]['web'], str):
            return f"[{nom_ville}]({self.towns[nom_ville]['web']})"
        else:
            return nom_ville

    def town_info(self,nom_ville):
        print(self.towns[nom_ville])

    def __iter__(self):
        for index, (nom_ville, infos) in enumerate(self.towns.items()):
            yield index, nom_ville, infos


class WayInfo:
    def __init__(self, distance, elevation, town, way, segment):
        self.distance = distance
        if elevation>=0:
            self.elevationPositif = elevation
            self.elevationNegatif = 0
        else:
            self.elevationPositif = 0
            self.elevationNegatif = elevation
        self.town = town
        self.way = way
        self.segment = segment
        self.title = None
        self.tags = None
        self.terrain = ""


    def print_info(self):
        print(f"Distance: {self.distance}, Elevation: {self.elevation}, Town: {self.town}, Segment: {self.segment}, Title: {self.title}")
        print(self.way)
        print(self.tags)
        #print(self.way['Name'].to_string())
        ##all_properties = dir(self)
        #properties = {prop: getattr(self, prop) for prop in all_properties if not callable(getattr(self, prop)) and not prop.startswith('__')}
        #print(properties)

    def print_object(obj):
        return True
        if(isinstance(obj,dict)):
            for key, value in obj.items():
                print(f"{key} : {value}")
        else:
            attributes = dir(obj)
            for attr in attributes:
                # Filtrer les méthodes et attributs spéciaux
                if not callable(getattr(obj, attr)) and not attr.startswith('__'):
                    print(f"{attr} : {getattr(obj, attr)}")

    def update_tags(self,tags):
        self.tags = tags


    def double_get(self,key):
        if self.tags:
            valeur = self.tags.get(key, '').lower()
            if valeur:
                return valeur
        valeur = self.way.get(key, '').lower()
        return valeur


    def find_terrain(self):

        if not is_osmid_positive(self.way['osmid']):
            return "Unknown"
        

        surface = self.double_get('surface')
        cycleway = self.double_get('cycleway')
        bicycle = self.double_get('bicycle')
        highway = self.double_get('highway')
        if 'name' in self.way:
            name = get_first_string(self.way['name']).lower()
        else:
            name = ""

        #if cycleway == 'yes' or cycleway == 'sidewalk' or highway == 'cycleway':
        if cycleway == 'yes' or cycleway == 'sidewalk' or bicycle == 'designated' or bicycle == 'yes' or highway == 'cycleway':
            if surface == 'gravel':
                pass
            elif highway == 'track':
                pass
            elif surface == 'compacted':
                return 'CompactedCycleway'
            else:
                return 'AsphaltedCycleway'

        if highway == 'unclassified':
            if 'route' in name:
                return  'SmallRoad'
            if 'rue' in name:
                return 'Street'
            if 'asphalt' in surface:
                return 'SmallRoad'
            source = self.way.get('source', '').lower()
            if 'bing' in source:
                return 'SmallRoad'
            if 'chemin' in name:
                return 'Track1'
            return 'Unclassified'

        if highway == 'motorway' or highway == 'trunk' or highway == 'primary' :
            return 'MainRoad'

        if highway == 'secondary' or highway == 'secondary_link':
            return 'SecondaryRoad'

        if highway == 'tertiary':
            return WayInfo.is_street(name,'SmallRoad')

        if highway == 'residential' or highway == 'living_street':
            return WayInfo.is_street(name,'Street')

        if highway == 'track':
            tracktype = self.double_get('tracktype')
            tracktype = tracktype.replace('grade','')
            if tracktype == "":
                return 'Track0'
            return 'Track'+tracktype

        if highway == 'path' or highway == 'footway' or highway == 'pedestrian' or highway == 'steps':
            return 'Path'

        if highway == 'proposed':
            proposed = self.double_get('proposed')
            if proposed == 'path':
                return 'Path'
            if proposed == 'track':
                return 'Track1'
            return 'Unclassified'

        if highway == 'service':
            tracktype = ""
            if self.double_get('tracktype'):
                tracktype = self.double_get('tracktype')
                tracktype = tracktype.replace('grade','')
                return 'Track'+tracktype
            if 'rue' in name:
                return 'Street'

            return 'Track1'

        return 'Unknown'


    def is_street(name,type):
        if 'boulevard' in name or 'avenue' in name:
            return 'MainStreet'
        if 'rue' or 'impasse' in name:
            return 'Street'
        return type


    def update_title(self):

        if not is_osmid_positive(self.way['osmid']):
            self.title = "Unknown"
            self.terrain = "Unknown"
            return True
        
        WayInfo.print_object(self.way)

        self.terrain = self.find_terrain()
        print(self.terrain+"\n")

        if 'name' in self.way:
            name = get_first_string(self.way['name']).lower()
            if name == "nan":
                name = ""
        else:
            name = ""

        if name:
            title = name
        else:
            if "Cycleway" in self.terrain:
                title = "piste cyclable"
            elif "Path" or "Trak0" in self.terrain:
                title = "chemin"
            elif "SecondaryRoad" == self.terrain:
                if self.distance<50:
                    title = "traversée départementale"
                else:
                    title = "départementale"
            elif "Road" in self.terrain:
                if self.distance<50:
                    title = "traversée route"
                else:
                    title = "route"
            else:
                title = self.terrain        

        #    if self.distance<50:
        #        highway = "traversée départementale"

        width = self.way.get('width')

        # elif any(mot in name.lower() for mot in ["rue", "avenue", "impasse"]):
        #     title = name 
        # else:
        #     title = f"{name} ({highway})"

        self.title = title[0].upper() + title[1:] if title else title


def is_in(search, variable):
    if isinstance(variable, str) and variable == search:
        return True
    elif isinstance(variable, list) and search in variable:
        return True
    else:
        return False


def is_osmid_positive(osmid):
    # Si osmid est un entier, vérifier s'il est positif
    if isinstance(osmid, int):
        return osmid > 0
    
    # Si osmid est une liste, vérifier si tous les éléments sont positifs
    elif isinstance(osmid, list):
        return all(item > 0 for item in osmid)
    
    # Retourne False si osmid n'est ni un entier ni une liste
    else:
        return False
    
def get_first_string(value):
    if isinstance(value, list):
        return value[0] if value else None
    return str(value)


def terrain_color(terrain):
    if terrain=='MainStreet' or terrain=='Street':
        return '#85C1E9' #Pale sky blue
    if terrain=='Road' or terrain=='MainRoad' or terrain=='SecondaryRoad' or terrain=='SmallRoad':
        return '#1E90FF' #Dodger Bleu (Spanish Green #009150) (Light sky blue #5DADE2)
    if terrain=='Cycleway' or terrain=='AsphaltedCycleway' or terrain=='CompactedCycleway':
        return '#AEF359' #Green lemon
    if terrain=='Track1' or terrain=='Track2' or terrain=='Track3' or terrain=='Track4' or terrain=='Track5' or terrain=='Track0':
        return '#FFD700' #Golden sun
    if terrain=='Path':
        return '#FF4500' #Sun red-orange
    if terrain=='Unclassified':
        return '#A569BD' #Purple twilight
    if terrain=='Unknown':
        return '#FD6C9E' #Rose
    return "#000000"
        

def terrain_order(terrain):
    if terrain=='MainStreet':
        return 1
    if terrain=='Street':
        return 2
    if terrain=='MainRoad':
        return 3
    if terrain=='SecondaryRoad':
        return 4
    if terrain=='SmallRoad' or terrain=='Road':
        return 5
    if terrain=='AsphaltedCycleway':
        return 6
    if terrain=='CompactedCycleway':
        return 7
    if terrain=='Track0':
        return 8
    if terrain=='Track1':
        return 9
    if terrain=='Track2':
        return 10
    if terrain=='Track3':
        return 11
    if terrain=='Track4':
        return 12
    if terrain=='Track5' or terrain=='Track':
        return 13
    if terrain=='Path':
        return 14
    if terrain=='Unclassified':
        return 15
    if terrain=='Unknown':
        return 16


def plot_communes_brut(communes_gdf):
    # Création de la figure et de l'axe pour le tracé
    fig, ax = plt.subplots(figsize=(10, 10))
    
    # Tracé des communes
    communes_gdf.plot(ax=ax, edgecolor='blue', facecolor='none')
    
    # Réglage des limites de l'affichage pour correspondre à celles des communes
    minx, miny, maxx, maxy = communes_gdf.total_bounds
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    
    # Optionnel: Suppression des axes pour une visualisation plus épurée
    ax.axis('off')
    
    # Affichage de la carte
    plt.show()


def plot_communes(path, communes_gdf, villes_info, gpx=None, title="Trace"):

    mode = 'default'
    if gpx:
        if hasattr(gpx, 'tracks'):
            mode = 'gpx'
        else:
            mode = 'info'

    bounds = communes_gdf.total_bounds
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    if width>height:
        aspect_ratio = width / height
        fig_height = 10
        fig_width = fig_height * aspect_ratio * 0.72
        ha_align = 'left'
        legend_position = (1.04, 0.5)
        adjust_params = {'left': 0, 'right': 1, 'top': 1, 'bottom': 0}
    else:
        fig_height = 10
        fig_width = 10
        ha_align = 'left'
        legend_position = (-0.2, 0)
        adjust_params = {'left': 0.3, 'right': 1, 'top': 0.95, 'bottom': 0.01}

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.set_xlim([bounds[0], bounds[2]])
    ax.set_ylim([bounds[1], bounds[3]])
    plt.subplots_adjust(**adjust_params)
    ax.set_facecolor('black')
    #plt.tight_layout(pad=0.1)

    if mode == 'info':
        communes_gdf.plot(ax=ax, edgecolor='#EEEEEE', facecolor='black', linewidth=0.5)
    else:
        communes_gdf.plot(ax=ax, edgecolor='blue', facecolor='lightblue', linewidth=0.5)
    liste_des_villes = list(villes_info.towns)

    for idx, row in communes_gdf.iterrows():

        # Récupérer le centre de chaque commune pour positionner le texte
        x, y = row['geometry'].centroid.x, row['geometry'].centroid.y

        if mode == 'default':
            nom_ville = row['name']        
            if nom_ville in villes_info:
                index_ville = liste_des_villes.index(nom_ville) + 1

                if index_ville == 1:
                    ax.text(x, y, str(index_ville), fontsize=5, ha='center', va='center', backgroundcolor='lightgreen')
                else:
                    ax.text(x, y, str(index_ville), fontsize=5, ha='center', va='center')

    mystats_surfaces = None
    if mode=='gpx':
        for track in gpx.tracks:
            for segment in track.segments:
                latitudes = [point.latitude for point in segment.points]
                longitudes = [point.longitude for point in segment.points]
                ax.plot(longitudes, latitudes, color='red', linewidth=3, alpha=0.5)
    elif mode=="info":
        for way in gpx:
            latitudes, longitudes = zip(*way.segment)  # Décompresse les tuples en deux listes
            color = terrain_color(way.terrain)  # Utilisez votre logique de couleur ici
            ax.plot(longitudes, latitudes, color=color, linewidth=3, alpha=1)

        # xx-small, x-small, small, medium, large, x-large, xx-large, larger, smaller, None
        stats = ways_stats(gpx)
        mystats_surfaces = stats_surfaces(stats)
        ax.legend(handles=make_legend(stats),
                  loc='lower right',
                  fontsize='x-large',
                  markerscale=0.6,
                  handletextpad=0.4,
                  labelspacing=0.4,
                  framealpha=1.0,
                  facecolor='white',
                  edgecolor='black',
                  bbox_to_anchor=legend_position)
                
    plt.text(0.01, 0.99, title, fontsize=38, fontweight='bold', ha=ha_align, va='top', transform=fig.transFigure)
    if mystats_surfaces:
        plt.text(0.01, 0.92, mystats_surfaces, fontsize=24, fontweight='normal', ha=ha_align, va='top', transform=fig.transFigure)

    ax.axis('off')

    #plt.savefig(path, bbox_inches='tight', dpi=600)
    plt.savefig(path, dpi=300)
    plt.close()


def title_element(title_text, stats, bilan):

    legend = ""
    for key, stat in stats:
        if stat['percent']==0:
            continue
        label = f"{key} {stat['percent']}%"
        color = terrain_color(key)
        legend += f"<div style='display:flex; align-items:center;'><div style='background:{color};width:1rem;height:1rem;margin-right:0.5rem;'></div>{label}</div>"


    title_html = f"""
        <div style="z-index:999;position: fixed; top: 1rem; right: 1rem; margin:0; padding:0.5rem; background-color: rgba(255,255,255,1); border-radius: 5px;">
            <h3 style="font-size:1.5rem">{title_text}</h3>{bilan.replace("\n","<br/>")}<br/><br/>{legend}
        </div>
         """
    title_element = Element(title_html)
    return title_element

def plot_communes_folium(path, communes_gdf, gpx=None, title="Trace"):

    mode = 'default'
    if gpx:
        if hasattr(gpx, 'tracks'):
            mode = 'gpx'
        else:
            mode = 'info'

    # https://deparkes.co.uk/2016/06/10/folium-map-tiles/
    # https://pypi.org/project/folium/0.1.4/
    m = folium.Map(tiles='CartoDB positron') #Fond casi blanc
    #m = folium.Map(tiles='CartoDB dark_matter') #Fond casi noir
    #m = folium.Map(tiles='OpenStreetMap') #Classique
    #m = folium.Map(tiles='CartoDB Voyager')

    bounds = communes_gdf.total_bounds
    m.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])

    # Ajouter les polygones des communes à la carte
    for index, commune in communes_gdf.iterrows():
        folium.GeoJson(
            data=commune['geometry'],
            style_function=lambda x: {'fillColor': 'gray', 'color': 'black', 'weight': 0.5},
        ).add_to(m)

    # Ajouter des traces GPX si disponibles
    if mode == "gpx":
        for track in gpx.tracks:
            for segment in track.segments:
                points = [(point.latitude, point.longitude) for point in segment.points]
                folium.PolyLine(points, color='red', weight=3, opacity=0.5).add_to(m)

    elif mode == "info":

        filter = 0.001
 
        # Grouper les segments par terrain
        for terrain, group in groupby(sorted(gpx, key=lambda x: x.terrain), key=lambda x: x.terrain):
            multiline = MultiLineString([way.segment for way in group])
            merged_line = linemerge(multiline)

            if isinstance(merged_line, LineString):
                coords = [list(merged_line.simplify(filter, preserve_topology=True).coords)]
            elif isinstance(merged_line, MultiLineString):
                coords = [list(line.simplify(filter, preserve_topology=True).coords) for line in merged_line.geoms]

            else:
                coords = []

            color = terrain_color(terrain)
            for coord in coords:
                folium.PolyLine(locations=coord,
                                color=color,
                                weight=4,
                                opacity=1,
                                bubblingMouseEvents=False).add_to(m)

        stats = ways_stats(gpx)
        mystats_surfaces = stats_surfaces(stats)
        m.get_root().html.add_child(title_element(title, stats, mystats_surfaces))


    # Ajout du script JavaScript pour activer la carte au clic
    script = """
    <script>
function disableBodyInteractions() {
    document.body.style.pointerEvents = 'none'; // Désactive toutes les interactions de pointeur sur le body
}

function enableBodyInteractions() {
    document.body.style.pointerEvents = 'auto'; // Réactive les interactions de pointeur
}

document.addEventListener('DOMContentLoaded', function() {
    disableBodyInteractions();

    window.addEventListener('focus', function() {
        enableInteractions();
    });
    window.addEventListener('blur', function() {
        disableInteractions();
    });
});
    </script>
    """
    m.get_root().html.add_child(Element(script))

    m.save(path)
    webbrowser.open( "file://" + path, new=2)


def locate_way(lat, lon, G_projected, flag_cache=True):

    hash = cache.create_hash((lat,lon), 'locate_way')
    if flag_cache:
        found, cached_result = cache.get_cache(hash)
        if found:
             return cached_result
    
    if not hasattr(locate_way, "previous_edge"):
        locate_way.previous_edge = None
        print("No cahe: locate_way")

    if not hasattr(locate_way, "nodes_gdf"):
        locate_way.nodes_gdf = osmnx.graph_to_gdfs(G_projected, nodes=True, edges=False)

    # Convertir le point en format Shapely et le segment GPX en LineString
    point_geom = Point(lon, lat)  # Conversion en (lon, lat)
    
    # Transformer les coordonnées si nécessaire
    gdf_point = gpd.GeoDataFrame(index=[0], crs='EPSG:4326', geometry=[point_geom])
    gdf_point_proj = gdf_point.to_crs(G_projected.graph['crs'])
    point_proj = gdf_point_proj.geometry.iloc[0]

    #Teste si point sur ancien edge
    if locate_way.previous_edge is not None:
        if 'geometry' in locate_way.previous_edge and locate_way.previous_edge['geometry'] is not None:
            distance = gdf_point_proj.distance(locate_way.previous_edge['geometry']).iloc[0]
            if distance<5:
                cache.into_cache(hash, locate_way.previous_edge)
                return locate_way.previous_edge
            
 
    # Trouver tous les nœuds dans le rayon R autour du point
    radius = 500
    #nodes_gdf = osmnx.graph_to_gdfs(G_projected, nodes=True, edges=False)
    locate_way.nodes_gdf['dist_to_point'] = locate_way.nodes_gdf.geometry.distance(point_proj)
    locate_way.nodes_within_radius = locate_way.nodes_gdf[locate_way.nodes_gdf['dist_to_point'] <= radius].index.tolist()

    min_distance = float('inf')

    nearest_edge = {'osmid': 0, 'name': '', 'dist_to_segment': None, 'nearest': None, 'geometry': None}

    # Parcourir les arêtes entre ces nœuds pour trouver la plus proche du segment GPX
    for u, v, data in G_projected.edges(data=True):
        if u in locate_way.nodes_within_radius or v in locate_way.nodes_within_radius:
            edge_geom = data['geometry'] if 'geometry' in data else LineString([Point(G_projected.nodes[n]['x'], G_projected.nodes[n]['y']) for n in (u, v)])

            distance = point_proj.distance(edge_geom)
            if distance < min_distance:
                min_distance = distance
                if 'geometry' not in nearest_edge:
                    nearest_edge['geometry'] = None
                nearest_edge = data
    
    locate_way.previous_edge = nearest_edge
    cache.into_cache(hash,nearest_edge)
    return nearest_edge


#Plus lens et moins précis que locate_way
def locate_way_path(gpx_segment, G_projected):

    hash = cache.create_hash(gpx_segment,'locate_segment')
    found, cached_result = cache.get_cache(hash)
    if found:
        return cached_result

    if not hasattr(locate_way_path, "previous_edge"):
        locate_way_path.previous_edge = None

    #invert ordre
    geometries = [(lon, lat) for lat, lon in gpx_segment]
    segment_line = LineString(geometries)
    gdf_segment = gpd.GeoDataFrame(geometry=[segment_line], crs='EPSG:4326')
    gdf_segment_proj = gdf_segment.to_crs(G_projected.graph['crs'])

    #Teste segment prolonge ancien edge
    if locate_way_path.previous_edge is not None and locate_way_path.previous_edge['geometry'] is not None:
        distance = gdf_segment_proj.distance(locate_way_path.previous_edge['geometry']).iloc[0]
        if distance<5:
            cache.into_cache(hash, locate_way_path.previous_edge)
            return locate_way_path.previous_edge

    buffer_distance = 5  # en mètres, ajuster selon le besoin
    gdf_segment_proj = gdf_segment_proj.buffer(buffer_distance)

    gdf_edges_proj = osmnx.utils_graph.graph_to_gdfs(G_projected, nodes=False, edges=True)
    gdf_edges_proj['dist_to_segment'] = gdf_edges_proj.distance(gdf_segment_proj.geometry.iloc[0])

    segment_orientation = calculate_orientation(segment_line)
    gdf_edges_proj['orientation'] = gdf_edges_proj['geometry'].apply(calculate_orientation)
    tolerance_degrees = 30  # par exemple, accepter une différence jusqu'à 30 degrés
    orientation_diff = np.abs(gdf_edges_proj['orientation'] - segment_orientation) % 360
    orientation_diff = np.minimum(360 - orientation_diff, orientation_diff)
    filtered_edges = gdf_edges_proj[orientation_diff <= tolerance_degrees]

    if not filtered_edges.empty:
        nearest_edge = filtered_edges.nsmallest(1, 'dist_to_segment').iloc[0]
        if nearest_edge['dist_to_segment']>5:
            nearest_edge = {'osmid': 0, 'orientation': segment_orientation, 'dist_to_segment': nearest_edge['dist_to_segment'], 'nearest': nearest_edge, 'geometry': None}
    else:
        nearest_edge = {'osmid': 0, 'orientation': segment_orientation, 'dist_to_segment': None, 'nearest': None, 'geometry': None}

    locate_way_path.previous_edge = nearest_edge

    cache.into_cache(hash, nearest_edge)

    return nearest_edge


# https://overpass-turbo.eu/
def overpass(query):

    hash = cache.create_hash(query,'overpass')
    found, cached_result = cache.get_cache(hash)
    if found:
        return cached_result
    
    # Endpoint de l'API Overpass
    url = "http://overpass-api.de/api/interpreter"

    response = requests.post(url, data={'data': query})
    data = response.json()
    
    cache.into_cache(hash, data['elements'])
    return data['elements']


def calculate_orientation(line):
    """
    Calcule l'orientation d'une ligne en degrés par rapport au Nord.
    """
    if isinstance(line, LineString) and len(line.coords) >= 2:
        x1, y1 = line.coords[0]
        x2, y2 = line.coords[-1]
        angle_rad = np.arctan2(y2 - y1, x2 - x1)
        angle_deg = np.degrees(angle_rad)
        orientation = (angle_deg + 360) % 360
        return orientation
    else:
        return None

def ways(frame):

    hash = cache.create_hash(frame,'ways')
    found, cached_result = cache.get_cache(hash)
    if found:
        return osmnx.project_graph(cached_result)
    
    gpx_bbox = (frame['max_lat'], frame['min_lat'], frame['max_lon'], frame['min_lon'])
    G = osmnx.graph_from_bbox(bbox=gpx_bbox, network_type='all')

    cache.into_cache(hash, G)
    #Passer en système métrique 
    return osmnx.project_graph(G)


def polygon_ways(polygon,flag_cache=True):

    hash = cache.create_hash(polygon,'polygon_ways')
    if flag_cache:
        found, cached_result = cache.get_cache(hash)
        if found:
            return osmnx.project_graph(cached_result)
    
    G = osmnx.graph_from_polygon(polygon, network_type='all')

    cache.into_cache(hash, G)
    #Passer en système métrique 
    return osmnx.project_graph(G)


def polygons_ways(polygons,flag_cache=True):

    hash = cache.create_hash(polygons,'polygons_ways')
    if flag_cache:
        found, cached_result = cache.get_cache(hash)
        if found:
            return cached_result

    #graph_list = []
    combined_graph = nx.MultiDiGraph()

    for polygon in polygons:
        graph = polygon_ways(polygon, flag_cache)
        combined_graph = nx.compose(combined_graph, graph)
        #graph_list.append(graph)

    #combined_graph = osmnx.utils_graph.combine_graphs(graph_list)
    combined_graph = osmnx.project_graph(combined_graph)
    cache.into_cache(hash, combined_graph)

    return combined_graph


def gpx_polygon(gpx):

    gpx_points = []

    for track in gpx.tracks:
        for segment in track.segments:
            for point in segment.points:
                gpx_points.append((point.longitude, point.latitude))

    if len(gpx_points) > 2:
        reverse_points = list(reversed(gpx_points[1:-1]))
        all_points = gpx_points + reverse_points
        convex_hull = Polygon(all_points)
        convex_hull = convex_hull.simplify(0.0001, preserve_topology=True)
        convex_hull = convex_hull.buffer(0.005)
        return convex_hull

        #multi_point = MultiPoint(gpx_points)
        #buffered_polygon = multi_point.convex_hull.buffer(buffer_distance)

    else:
        exit("Pas assez de points pour former un polygone.")


def gpx_polygons(gpx, meters, max_distance_km=300):
    polygons = []
    current_segment = []
    
    # Initialiser le point précédent
    prev_point = None
    distance_enter = 0

    for track in gpx.tracks:
        for segment in track.segments:
            for i, point in enumerate(segment.points):
                if prev_point is None:
                    # Premier point du segment
                    current_segment.append((point.longitude, point.latitude))
                else:
                    # Calculer la distance depuis le dernier point
                    #distance = geopy.distance.distance(prev_point, (point.latitude, point.longitude)).km
                    distance = meters[i]-distance_enter
                    if distance > max_distance_km*1000:
                        # Distance maximale atteinte, créer un polygone avec le segment actuel
                        if len(current_segment) > 2:
                            poly = Polygon(LineString(current_segment).buffer(0.01))  # 0.01 degré de buffer
                            polygons.append(poly)
                            current_segment = [(point.longitude, point.latitude)]
                            distance_enter = meters[i]
                    else:
                        # Ajouter le point au segment actuel
                        current_segment.append((point.longitude, point.latitude))
                prev_point = (point.latitude, point.longitude)
    
    # Traiter le dernier segment
    if len(current_segment) > 2:
        poly = Polygon(LineString(current_segment).buffer(0.01))  # Ajuster le buffer selon le besoin
        polygons.append(poly)
    
    return polygons


def show_polygons(polygons):
    if polygons:
        central_point = list(polygons[0].exterior.coords)[0]

        # Initialisation de la carte Folium au point central
        m = folium.Map(location=[central_point[1], central_point[0]], zoom_start=12)

        # Ajout de chaque polygone à la carte
        for poly in polygons:
            # Conversion des coordonnées du polygone pour Folium ([latitude, longitude])
            poly_coords = [[p[1], p[0]] for p in poly.exterior.coords]
            folium.Polygon(locations=poly_coords,
                        color='blue',
                        weight=2,
                        fill_color='red',
                        fill_opacity=0.5).add_to(m)

        map_file = os.path.join(output_folder, "_map.html")
        m.save(map_file)
        webbrowser.open( "file://" + map_file, new=2)


def show_polygon(polygone):
    gdf = gpd.GeoDataFrame(index=[0], geometry=[polygone])
    fig, ax = plt.subplots()
    gdf.plot(ax=ax, color='lightblue', edgecolor='black')
    plt.show()


def gpx_frame(gpx):
    # Adapter selon les dimensions réelles de votre tracé
    min_lat, max_lat, min_lon, max_lon = float('inf'), float('-inf'), float('inf'), float('-inf')
    for track in gpx.tracks:
        for segment in track.segments:
            for point in segment.points:
                min_lat, max_lat = min(min_lat, point.latitude), max(max_lat, point.latitude)
                min_lon, max_lon = min(min_lon, point.longitude), max(max_lon, point.longitude)

    # Calcul de la moyenne de latitude pour l'ajustement de la longitude
    mean_lat = (min_lat + max_lat) / 2.0
    # Conversion de 100m en degrés de latitude et de longitude
    lat_adjustment = 100 / 111000
    lon_adjustment = 100 / (111000 * math.cos(math.radians(mean_lat)))
    
    # Ajuster les limites (100 mètres de marges tout autour)
    min_lat -= lat_adjustment
    max_lat += lat_adjustment
    min_lon -= lon_adjustment
    max_lon += lon_adjustment

    return {"min_lat":min_lat, "max_lat":max_lat, "min_lon":min_lon, "max_lon":max_lon}


def frame_corners(frame):
    corners = (
        (frame['max_lat'], frame['min_lon']), #haut-droit
        (frame['min_lat'], frame['max_lon']), #bas-gauche
        (frame['min_lat'], frame['min_lon']), #bas-droite
        (frame['max_lat'], frame['max_lon']), #haut-gauche
    )
    return corners


def ways_stats(ways_info):
    stats = {}
    total_distance = 0
    for way in ways_info:
        if way.terrain in stats:
            stats[way.terrain]['distance'] += way.distance
            stats[way.terrain]['elevation'] += way.elevationPositif
        else:
            stats[way.terrain] = {
                'distance': way.distance,
                'elevation': way.elevationPositif,
                'dformat': 0,
                'color': terrain_color(way.terrain),
                'percent':0,
                'order':terrain_order(way.terrain)
            }
        total_distance += way.distance

    for key, stat in stats.items():
        stats[key]['dformat'] = distance_lisible(stats[key]['distance'])
        stats[key]['percent'] = round(stats[key]['distance']*100/total_distance)

    stats_sorted = sorted(stats.items(), key=lambda x: x[1]['order'] if x[1]['order'] is not None else float('inf'))
    return stats_sorted


def distance_lisible(distance_m):
    distance_km = distance_m / 1000.0  # Convertir en kilomètres
    if distance_m < 100:
        distance = round(distance_m)
        # Format avec une décimale
        return f"{distance}m"
    elif distance_km < 10:
        # Format avec une décimale
        return f"{distance_km:.1f}km"
    else:
        # Format sans décimale, arrondi à l'entier le plus proche
        return f"{round(distance_km)}km"

def elevation_lisible(elevation_m):
    elevation = round(elevation_m)
    if elevation < 100:
        return f"{elevation}m"
    elif elevation < 1000:
        return f"{round(elevation,-1)}m"
    else:
        return f"{round(elevation,-2)}m"

def make_legend(stats):
    handles = []
    for key, stat in stats:
        if stat['percent']==0:
            continue
        label = f"{key} {stat['percent']}%"
        handles.append(mpatches.Patch(color=stat['color'], label=label))
    return handles


def stats_surfaces(stats_input):

    total_elevation = sum(way['elevation'] for key, way in stats_input)
    total_distance = sum(way['distance'] for key, way in stats_input)
    stats = {key: way for key, way in stats_input}


    street = stats.get('Street', {}).get('percent', 0) + stats.get('MainStreet', {}).get('percent', 0)
    road = stats.get('SmallRoad', {}).get('percent',0) + stats.get('SecondaryRoad', {}).get('percent',0) + stats.get('MainRoad', {}).get('percent',0)
    #cycleway = stats['AsphaltedCycleway']['distance']+stats['CompactedCycleway']['distance']
    #track = stats['Track1']['distance']+stats['Track2']['distance']+stats['Track3']['distance']+stats['Track4']['distance']+stats['Track5']['distance']+stats['Track0']['distance']
    path = stats.get('Path', {}).get('percent',0) + stats.get('Unknown', {}).get('percent',0) + stats.get('Unclassified', {}).get('percent',0)

    asphalt = stats.get('AsphaltedCycleway', {}).get('percent',0) + street + road
    ground = 100 - asphalt - path
    #ground = track + path + stats['CompactedCycleway']

    km = distance_lisible(total_distance)
    return f"{km}\n+{elevation_lisible(total_elevation)}\n{asphalt}% asphalt\n{ground}% tracks\n{path}% single tracks"

def locate_point_in_town(lat, lon, towns_gdf):

    hash = cache.create_hash((lat,lon),'locate_point')
    found, cached_result = cache.get_cache(hash)
    if found:
        return cached_result
    
    location = Point(lon, lat)
    
    # Vérifier dans quelle commune se trouve le point
    for _, row in towns_gdf.iterrows():
        if location.within(row['geometry']):
            response = (row['name'], row['postal_code'], row['population'], row['website'])
            cache.into_cache(hash, response)
            return response
    
    return ""


def gpx_villes(gpx,meters,towns_gdf,towns_info):

    total_points = sum(len(segment.points) for track in gpx.tracks for segment in track.segments)
    pbar = tqdm(total=total_points, desc='Towns in GPX:')

    now_town = None
    now_town_name = None

    for track in gpx.tracks:
        for segment in track.segments:
            points = segment.points
            for i, point in enumerate(points):

                pbar.update(1)

                #filter
                if i>0 and i<total_points-2 and meters[i]-meters[last_point]<200:
                    continue

                last_point = i
                town_tuple = locate_point_in_town(point.latitude, point.longitude, towns_gdf)
                if town_tuple[0] != now_town_name:
                    if now_town:
                        #On quitte la now_town
                        (town_name,postal_code,population,website) = now_town
                        distance = meters[i]-distance_enter_town
                        distance_enter = distance_enter_town
                        towns_info.update_town(town_name, postal_code, population, website, distance_enter, distance)
                        
                    #On entre a new town
                    distance_enter_town = meters[i]
                    now_town = town_tuple
                    now_town_name = now_town[0]

    distance = meters[i-1]-distance_enter_town
    (town_name,postal_code,population,website) = town_tuple
    towns_info.update_town(town_name, postal_code, population, website, distance_enter_town, distance)
                
    pbar.close()


def cities(frame):

    hash = cache.create_hash(frame,'cities')
    found, cached_result = cache.get_cache(hash)
    if found:
        return cached_result

    gpx_bbox = (frame['max_lat'], frame['min_lat'], frame['max_lon'], frame['min_lon'])
    communes_gdf = osmnx.features.features_from_bbox(bbox=gpx_bbox, tags={'admin_level': '8'})

    cache.into_cache(hash,communes_gdf)
    return communes_gdf


def osm_test():
    osmnx.config(use_cache=True, log_console=True)
    G = osmnx.graph_from_place('Balaruc les Bains, Hérault, France', network_type='drive')
    osmnx.plot_graph(osmnx.project_graph(G))


def meter_2_km(m):
    return round(m/1000)