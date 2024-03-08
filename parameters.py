import os

script_dir = os.path.dirname(os.path.abspath(__file__)) + os.sep
assets_dir = f"{script_dir}assets/" 
output_folder = f"{script_dir}output/"

images_folder = os.path.expanduser("~/Documents/GitHub/727/images/")
gpx_path = f"{assets_dir}route.gpx"
cover_img = f"{assets_dir}cover.png"
audio_file = f"{assets_dir}music.mp3"
cache_file = f"{script_dir}cache/_img2gpx_cache.pkl"
font_file = f"{assets_dir}Geneva.ttf"
img_trace = os.path.join(output_folder, "_trace.png")
video_file = os.path.join(output_folder, "_video.mp4")

distance_filter = 100 #Ignorer image plus loin de la trace

def print_params():
    print("script_dir",script_dir)
    print("cache_file",cache_file)

if __name__ == "__main__":
    print_params()