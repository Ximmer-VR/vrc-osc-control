cd src
pyinstaller --distpath ../dist --workpath ../build --clean --onefile --windowed --icon=resource/icon.ico --add-data "resource/icon.ico:resource" ./osccontrol.py
cd ..