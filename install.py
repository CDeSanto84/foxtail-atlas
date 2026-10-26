#!/usr/bin/env python3
"""Install Foxtail Atlas into this user's existing Foxtail desktop."""
from pathlib import Path
from datetime import datetime
import configparser
import shutil
import subprocess

HOME = Path.home()
SOURCE = Path(__file__).resolve().parent
APP = HOME / '.local/lib/foxtail-atlas'
BACKUP = SOURCE.parents[1] / 'work/atlas/install-backups' / datetime.now().strftime('%Y%m%d-%H%M%S')
BACKUP.mkdir(parents=True)


def save(path):
    if path.exists():
        destination = BACKUP / path.relative_to(HOME)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def write(path, text, executable=False):
    save(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if executable:
        path.chmod(0o755)


if APP.exists():
    shutil.copytree(APP, BACKUP / 'previous-application', ignore=shutil.ignore_patterns('__pycache__'))
APP.mkdir(parents=True, exist_ok=True)
shutil.copy2(SOURCE / 'server.py', APP / 'server.py')
shutil.copytree(SOURCE / 'static', APP / 'static', dirs_exist_ok=True)
shutil.copy2(SOURCE / 'README.md', APP / 'README.md')
icon = HOME / '.local/share/icons/foxtail-atlas.png'
save(icon)
shutil.copy2(SOURCE / 'static/atlas-icon.png', icon)

# The first city snapshot is reused so opening the app does not spend another credit.
staged = SOURCE.parents[1] / 'work/atlas/state'
state = HOME / '.local/state/foxtail-atlas'
state.mkdir(parents=True, exist_ok=True, mode=0o700)
if staged.is_dir():
    for original in staged.rglob('*.json'):
        destination = state / original.relative_to(staged)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(original, destination)
            destination.chmod(0o600)

service = HOME / '.config/systemd/user/foxtail-atlas.service'
write(service, f'''[Unit]
Description=Foxtail Atlas local map service
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
WorkingDirectory={APP}
ExecStart=/usr/bin/python3 {APP}/server.py --port 18864
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=2
TimeoutStopSec=5
''')

# The desktop session owns Chrome so it outlives a launcher shell.
desktop_service = HOME / '.config/systemd/user/foxtail-atlas-desktop.service'
write(desktop_service, f'''[Unit]
Description=Foxtail Atlas desktop window
PartOf=graphical-session.target
Wants=foxtail-atlas.service
After=foxtail-atlas.service

[Service]
Type=exec
ExecStart=/usr/bin/google-chrome --user-data-dir={HOME}/.config/foxtail-webapps/foxtail-atlas --no-first-run --no-default-browser-check --use-system-title-frame --class=foxtail-atlas --name=foxtail-atlas --window-position=120,90 --window-size=1540,1040 --app=http://127.0.0.1:18864/
KillMode=control-group
TimeoutStopSec=10
Restart=no
StandardOutput=append:{state}/desktop.log
StandardError=append:{state}/desktop.log
''')

launcher = HOME / '.local/bin/foxtail-atlas'
write(launcher, f'''#!/usr/bin/env bash
set -euo pipefail
if wmctrl -x -a foxtail-atlas 2>/dev/null; then
  exit 0
fi
systemctl --user start foxtail-atlas.service
/usr/bin/python3 - <<'READY'
import time
from urllib.request import urlopen
for attempt in range(40):
    try:
        with urlopen('http://127.0.0.1:18864/api/health', timeout=1) as response:
            if response.status == 200:
                break
    except Exception:
        time.sleep(.15)
else:
    raise SystemExit('Foxtail Atlas did not start.')
READY
exec systemctl --user start foxtail-atlas-desktop.service
''', executable=True)

desktop = HOME / '.local/share/applications/foxtail-atlas.desktop'
write(desktop, f'''[Desktop Entry]
Version=1.0
Type=Application
Name=Foxtail Atlas
GenericName=Public Internet Map Explorer
Comment=Explore indexed public IPs and providers on a geographic map
Exec={launcher}
Icon={icon}
Terminal=false
Categories=Network;Geography;
Keywords=atlas;map;navigation;internet;IPv4;IP;Shodan;providers;geolocation;
StartupNotify=true
StartupWMClass=foxtail-atlas
''')

geometry = HOME / '.config/foxtail-webapps/foxtail-atlas-geometry.env'
if not geometry.exists():
    write(geometry, 'ROGER_X=120\nROGER_Y=90\nROGER_WIDTH=1540\nROGER_HEIGHT=1040\n')

# Add to the curated Navigation folder rather than relying on generic categories.
navigation = HOME / '.local/bin/foxtail-launchpad-mode.base'
source = navigation.read_text()
if '"foxtail-atlas.desktop"' not in source:
    if '"Navigation": [\n' not in source:
        raise SystemExit('The Foxtail Navigation folder layout changed; launcher placement needs review.')
    updated = source.replace('"Navigation": [\n', '"Navigation": [\n        "foxtail-atlas.desktop",\n', 1)
    compile(updated, str(navigation), 'exec')
    write(navigation, updated, executable=True)

# Associate the app window with its new glass dock icon and native moon frame.
rule_path = HOME / '.config/kwinrulesrc'
save(rule_path)
rules = configparser.ConfigParser(interpolation=None, strict=False)
rules.optionxform = str
rules.read(rule_path)
name = 'foxtail-atlas-native'
rules[name] = {'Description':'Foxtail Atlas — native Moon frame and launcher identity',
               'desktopfile':'foxtail-atlas', 'desktopfilerule':'2', 'noborder':'false',
               'noborderrule':'2', 'types':'1', 'wmclass':'foxtail-atlas',
               'wmclasscomplete':'false', 'wmclassmatch':'1'}
order = [x for x in rules['General'].get('rules', '').split(',') if x and x != name]
rules['General']['rules'] = ','.join([name] + order)
rules['General']['count'] = str(len(order) + 1)
with rule_path.open('w') as handle:
    rules.write(handle, space_around_delimiters=False)

subprocess.run(['systemctl','--user','daemon-reload'], check=True)
subprocess.run(['systemctl','--user','restart','foxtail-atlas.service'], check=True)
subprocess.run(['qdbus6','org.kde.KWin','/KWin','reconfigure'], check=True)
subprocess.run(['update-desktop-database',str(HOME/'.local/share/applications')], check=True)
subprocess.run(['kbuildsycoca6','--noincremental'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
print('Installed Foxtail Atlas 0.2 in Navigation.')
print('Application:', APP)
print('Launcher:', launcher)
print('Icon:', icon)
print('Backup:', BACKUP)
