import gpxpy
import folium
#from parameters import gpx_path, cache_file, output_folder, logs_file
import numpy as np   
import pandas as pd 
import folium 
  
# define the world map 
world_map = folium.Map() 
  
# create a Stamen Toner map of the world 
# centered around Mumbai 
world_map = folium.Map(location =[19.11763765873, 72.9060384756],  
                       zoom_start = 10, tiles ='NASAGIBS Blue Marble') 
  
# display map 
world_map 
exit()

# Charger le fichier GPX
gpx_file = open(gpx_path, 'r')
gpx = gpxpy.parse(gpx_file)

# Créer une carte centrée sur le premier point de l'itinéraire
premier_point = gpx.tracks[0].segments[0].points[0]
carte = folium.Map(location=[premier_point.latitude, premier_point.longitude], zoom_start=14)

# Tracer l'itinéraire
for track in gpx.tracks:
    for segment in track.segments:
        points = [(point.latitude, point.longitude) for point in segment.points]
        folium.PolyLine(points).add_to(carte)

# Sauvegarder la carte dans un fichier HTML
carte.save('itineraire.html')
