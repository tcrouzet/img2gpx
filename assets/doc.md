https://osmnx.readthedocs.io/en/stable/user-reference.html#
https://overpass-turbo.eu/
https://github.com/gboeing/osmnx

python -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
brew install spatialindex geos proj (indispensable osmx)

deactivate

Pour mettre à jour venv
./venv/bin/pip install --upgrade pip setuptools wheel


