import shelve
import atexit
import hashlib, json
from shapely.geometry.base import BaseGeometry
from shapely.geometry import mapping
import pickle
import os
    

def init_cache(name, mode = "pickle"):
    if not hasattr(init_cache, "cache"):

        script_dir = os.path.dirname(os.path.abspath(__file__))
        init_cache.cache_dir = os.path.join(script_dir,"_cache")

        if not os.path.exists(init_cache.cache_dir):
            os.makedirs(init_cache.cache_dir)
        
        init_cache.cache_file =  os.path.join(init_cache.cache_dir,name.replace(".","-"))
        init_cache.mode = mode

        if init_cache.mode == "shelve":
            init_cache.cache = shelve.open(init_cache.cache_file, writeback=True)

        if init_cache.mode == "pickle":
            init_cache.cache_file += ".pkl"
            if os.path.exists(init_cache.cache_file):
                with open(init_cache.cache_file, 'rb') as fichier_cache:
                    init_cache.cache = pickle.load(fichier_cache)
            else:
                init_cache.cache = {}

        atexit.register(close_cache)

def get_foler():
    return init_cache.cache_dir

def save_cache():
    if hasattr(init_cache, "cache"):

        if init_cache.mode == "pickle":
            with open(init_cache.cache_file, 'wb') as fichier_cache:
                pickle.dump(init_cache.cache, fichier_cache)


def close_cache():
    if hasattr(init_cache, "cache"):

        if init_cache.mode == "pickle":
            save_cache()

        if init_cache.mode == "shelve":
            init_cache.cache.close()
    
        delattr(init_cache, "cache")
        print("Cache closed")


def get_cache(hash):
    if hasattr(init_cache, "cache"):
        if hash in init_cache.cache:
           return True, init_cache.cache[hash]
    return False, None


def into_cache(hash,value):
    if hasattr(init_cache, "cache"):
        init_cache.cache[hash]=value


def create_hash(data, suffix=""):
    try:
        # Essayer de sérialiser directement les données en JSON
        data_str = json.dumps(data, sort_keys=True) + suffix
    except TypeError:
        # Gérer les objets géométriques Shapely
        if isinstance(data, BaseGeometry):
            data_str = data.wkt  + suffix
        else:
            data_str = str(data) + suffix
    
    return hashlib.sha256(data_str.encode()).hexdigest()