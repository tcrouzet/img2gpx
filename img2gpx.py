import os
from PIL import Image, ImageDraw, ImageFont
from PIL.ExifTags import TAGS, GPSTAGS
import gpxpy.gpx
from moviepy.editor import ImageClip, concatenate_videoclips, CompositeVideoClip, AudioFileClip
import numpy as np
from datetime import datetime
from parameters import images_folder, cover_img, font_file, audio_file, distance_filter, img_trace, video_file, gpx_file, gpx_path
import tools as t
import osm_tools as o
import cache_manager as cache
import locale
locale.setlocale(locale.LC_TIME, 'fr_FR')

os.system('clear')
cache.init_cache(gpx_file)

def get_exif_data(image_path):

    img_hash = cache.create_hash(image_path,'iamge')
    found, cached_result = cache.get_cache(img_hash)
    if found:
        return cached_result

    image = Image.open(image_path)
    exif_data = {}
    
    if hasattr(image, '_getexif'):
        exif_info = image._getexif()
        if exif_info is not None:
            for tag, value in exif_info.items():
                decoded = TAGS.get(tag, tag)
                exif_data[decoded] = value

    cache.into_cache(img_hash, exif_data)
    return exif_data


def get_geotagging(exif_data):
    if 'GPSInfo' in exif_data:
        gps_info = exif_data['GPSInfo']
        
        geotagging = {}
        for tag in gps_info.keys():
            decoded = GPSTAGS.get(tag, tag)
            geotagging[decoded] = gps_info[tag]

        return geotagging
    return None


def convert_to_degrees(value):
    """ Convertit une valeur DMS EXIF en degrés décimaux """
    # Convertit chaque valeur IFDRational en un nombre décimal
    d, m, s = value
    return round(float(d) + float(m) / 60 + float(s) / 3600,5)


def get_decimal_coords(geotags):
    lat = convert_to_degrees(geotags['GPSLatitude'])
    lon = convert_to_degrees(geotags['GPSLongitude'])

    if geotags['GPSLatitudeRef'] != 'N':
        lat = -lat
    if geotags['GPSLongitudeRef'] != 'E':
        lon = -lon

    return (lat, lon)


# Fonction pour convertir les coordonnées GPS en coordonnées de l'image
def gps_to_image_coords(lat, lon, frame, image_size):
    x = (lon - frame['min_lon']) / (frame['max_lon'] - frame['min_lon']) * (image_size[0] - 1)
    y = (1 - (lat - frame['min_lat']) / (frame['max_lat'] - frame['min_lat'])) * (image_size[1] - 1)
    return x, y


def calculate_pixel_distance(pt1, pt2):
    return ((pt1[0] - pt2[0]) ** 2 + (pt1[1] - pt2[1]) ** 2) ** 0.5


def create_gpx_trace_image_segment(gpx, image_size, color, frame, output_path):
    
    w, h = image_size
    scale_factor = 1
    new_size = (w*scale_factor, h*scale_factor)
    # Créer une image transparente
    #img = Image.new('RGBA', image_size, (255, 255, 255, 0))
    img = Image.new('RGBA', new_size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    
    # Dessiner le tracé sur l'image
    min_distance_pixels = 20
    for track in gpx.tracks:
        for segment in track.segments:
            points = segment.points
            prev_point = None
            for point in points:
                if prev_point is not None:
                    new_point = gps_to_image_coords(point.latitude, point.longitude, frame, new_size)
                    if calculate_pixel_distance(prev_point, new_point) > min_distance_pixels:
                        draw.line([prev_point, new_point], fill=color, width=10*scale_factor)
                        prev_point = gps_to_image_coords(point.latitude, point.longitude, frame, new_size)
                else:
                    prev_point = gps_to_image_coords(point.latitude, point.longitude, frame, new_size)
    
    #img.save(output_path)
    if scale_factor>1:
        img = img.resize(image_size, Image.Resampling.LANCZOS)
    img.save(output_path)



def process_img(folder, gpx, meters, towns):
    global distance_filter, gdf

    images = []

    # Parcours du dossier source pour copier les images
    for root, _, files in os.walk(folder):
        for filename in files:
            if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp')):
                source_path = os.path.join(root, filename)

                exif_data = get_exif_data(source_path)
                geotags = get_geotagging(exif_data)
 
                if geotags is not None:
                    (lat, lon) = get_decimal_coords(geotags)
                    (distance,from_start) = t.calculate_distance(gpx, meters, lat, lon)

                    if distance>distance_filter:
                        continue

                    datetime = exif_data['DateTime']
                    width = exif_data['ExifImageWidth']
                    height = exif_data['ExifImageHeight']
                    (town, postal_code, population, website) = o.locate_point_in_town(lat,lon,towns)

                else:
                    continue
                
                dic = {
                    "path" : source_path, 
                    "latitude" : lat,
                    "longitude" : lon,
                    "distance" : distance,
                    "meters" : from_start,
                    "datetime": datetime,
                    "width": width,
                    "height": height,
                    "town": town
                }

                images.append(dic)

    return images


gpx = t.gpx_reader(gpx_path)
meters = t.gpx_meters(gpx)
frame = o.gpx_frame(gpx)
towns = o.cities(frame)
images = process_img(images_folder, gpx, meters, towns)
images = sorted(images, key=lambda dico: dico["meters"])

#Création de l'image de la trace
#taille_cible = (1920, 1440)
#taille_cible = (4800, 3600)
taille_cible = (1600, 1200)
#color = (255, 0, 0, 255) #Red
color = (255, 255, 255, 255) #white
shadow = "black"

create_gpx_trace_image_segment(gpx, taille_cible, color, frame, img_trace)
text_position = (10, 10)
padding = 10
#shadow_position = (12, 12)

clips = []
duree_image = 1.5
rayon_du_point = 30
font = ImageFont.truetype(font_file, 40)


width, height = taille_cible
duration = 0
for image in images:
    #print(image)
    #exit()
    img_name = image["path"].replace(images_folder,"")

    img = Image.open(image["path"])
    img_resized = img.resize(taille_cible, Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(img_resized)
    x, y = gps_to_image_coords(image["latitude"], image["longitude"], frame, taille_cible)

    # Dessiner le point
    draw.ellipse((x-rayon_du_point, y-rayon_du_point, x+rayon_du_point, y+rayon_du_point), fill=color)

    # Texte
    legend = image["town"]
    if legend:
        legend +=", "
    date_obj = datetime.strptime(image["datetime"], '%Y:%m:%d %H:%M:%S')
    legend += date_obj.strftime('%B %Y')
    #legend += img_name

    bbox = draw.textbbox(text_position, legend, font=font)
    background_position = [
        bbox[0] - padding,
        bbox[1] - padding,
        bbox[2] + padding,
        bbox[3] + padding
    ]
    #draw.text(shadow_position, legend, font=font, fill=shadow)
    draw.rectangle(background_position, fill=shadow)
    draw.text(text_position, legend, font=font, fill=color)

    img_np = np.array(img_resized)
    clip = ImageClip(img_np).set_duration(duree_image)
    clips.append(clip)
    duration += duree_image

video_fond = concatenate_videoclips(clips, method="compose")

trace_clip = ImageClip(img_trace).set_duration(duration)
first_end_clip = ImageClip(cover_img).set_duration(duree_image*2)

clip_final = CompositeVideoClip([video_fond, trace_clip])

clips_sequence = [first_end_clip, clip_final, first_end_clip]
clip_final = concatenate_videoclips(clips_sequence, method="compose")

if False:
    audio_clip = AudioFileClip(audio_file)
    audio_clip = audio_clip.subclip(0, clip_final.duration)
    audio_clip = audio_clip.audio_fadeout(duree_image*3)
    clip_final = clip_final.set_audio(audio_clip)

clip_final.write_videofile(video_file, codec="libx264", fps=24)        