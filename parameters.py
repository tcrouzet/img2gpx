import os

script_dir = os.path.dirname(os.path.abspath(__file__)) + os.sep
assets_dir = f"{script_dir}assets/" 
output_folder = f"{script_dir}_output/"
cache_folder = f"{script_dir}_cache/"
os.makedirs(output_folder, exist_ok=True)
os.makedirs(cache_folder, exist_ok=True)


images_root = os.path.expanduser("~/Documents/GitHub/727/")
images_folder = os.path.join(images_root,'images/')
cover_img = os.path.join(assets_dir, "cover.png")
audio_file = os.path.join(assets_dir,"music.mp3")
cache_file = os.path.join(cache_folder,"_img2gpx_cache")
font_file = os.path.join(assets_dir, "Geneva.ttf")
img_trace = os.path.join(output_folder, "_trace.png")
video_file = os.path.join(output_folder, "_video.mp4")
logs_file = os.path.join(script_dir, "_logs.txt")

gpx_file="i727.gpx"
#gpx_file="route-balaruc.gpx"
#gpx_file="route-tourmagne.gpx"
gpx_path = os.path.join(assets_dir, gpx_file)

distance_filter = 100 #Ignorer image plus loin de la trace

def print_params():
    print("script_dir",script_dir)
    print("cache_file",cache_file)

if __name__ == "__main__":
    print_params()