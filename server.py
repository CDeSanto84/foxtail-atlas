#!/usr/bin/env python3
"""Foxtail Atlas 0.2 — a local, map-first viewer of indexed public IPs."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import secrets
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
VERSION = '0.2.0'
STATE = Path(os.environ.get('ATLAS_STATE_DIR', str(Path.home() / '.local/state/foxtail-atlas')))
KEY_FILE = Path.home() / '.config/shodan/api_key'
DEFAULT_PLACE = {'name': 'Coral Springs, Florida', 'center': [26.2712, -80.2706],
                 'bounds': [[26.2296745, -80.2974021], [26.3109974, -80.2016082]]}
CACHE_TTL = 86400
LOCK = threading.RLock()
TOKEN = secrets.token_urlsafe(32)
GEOCODER_LOCK = threading.Lock()
LAST_GEOCODE = 0.0
ACCOUNT = {'value': None, 'at': 0.0}
CATALOG = {'signature': None, 'pages': [], 'hosts': []}


class AppError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def now():
    return datetime.now(timezone.utc).isoformat()


def remote_json(url, headers=None):
    try:
        req = Request(url, headers={'User-Agent': 'FoxtailAtlas/0.2 (local desktop map explorer)', **(headers or {})})
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code == 401:
            raise AppError('Shodan could not authenticate. Check the existing Shodan API key.', 502) from None
        if exc.code == 403:
            raise AppError('The provider declined this request. Check account access or available credits.', 502) from None
        if exc.code == 429:
            raise AppError('The provider is rate limiting requests. Please try again in a moment.', 429) from None
        if exc.code == 404:
            raise AppError('No indexed record is available for this IP.', 404) from None
        raise AppError(f'The data provider returned HTTP {exc.code}. Please try again.', 502) from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise AppError('The data provider could not be reached. Your saved map results are still available.', 502) from None


def shodan(path, **params):
    try:
        key = KEY_FILE.read_text().strip()
    except OSError:
        raise AppError('No Shodan connection is configured on this computer.', 503) from None
    if not key:
        raise AppError('The configured Shodan API key is empty.', 503)
    result = remote_json('https://api.shodan.io/' + path + '?' + urlencode({'key': key, **params}))
    if result.get('error'):
        # Never forward provider errors: they can contain authenticated URLs.
        raise AppError('Shodan could not complete this request. Check the selected area and account access.', 502)
    return result


def account():
    if ACCOUNT['value'] is not None and time.time() - ACCOUNT['at'] < 60:
        return ACCOUNT['value']
    try:
        data = shodan('api-info')
        result = {'connected': True, 'query_credits': data.get('query_credits'), 'plan': data.get('plan')}
    except AppError as exc:
        result = {'connected': False, 'message': exc.message}
    ACCOUNT.update(value=result, at=time.time())
    return result


def validate_bounds(value, search=True):
    try:
        south, west, north, east = [float(x) for x in value]
    except (ValueError, TypeError):
        raise AppError('Choose a valid map area.') from None
    if not all(math.isfinite(x) for x in [south, west, north, east]):
        raise AppError('Map coordinates must be finite.')
    if not (-85 <= south < north <= 85 and -180 <= west < east <= 180):
        raise AppError('Choose a map area that does not cross the date line.')
    if search and (north - south > 8 or east - west > 8):
        raise AppError('Zoom in to a city or region before exploring its IPs.')
    result = tuple(round(x, 4) for x in [south, west, north, east])
    if result[0] >= result[2] or result[1] >= result[3]:
        raise AppError('This map area is too small. Zoom out a little.')
    return result


def query_for(bounds):
    south, west, north, east = validate_bounds(bounds)
    return f'geo:{north},{west},{south},{east}'


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    temporary.chmod(0o600)
    temporary.replace(path)


def cache_path(query, page):
    digest = hashlib.sha256((query + ':' + str(page)).encode()).hexdigest()[:24]
    return STATE / 'areas' / (digest + '.json')


def normalize(matches):
    hosts = {}
    for item in matches:
        ip = item.get('ip_str', '')
        try:
            address = ipaddress.ip_address(ip)
            if address.version != 4 or not address.is_global:
                continue
        except ValueError:
            continue
        loc = item.get('location') or {}
        lat, lon = loc.get('latitude'), loc.get('longitude')
        if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
                and math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            lat = lon = None
        row = hosts.setdefault(ip, {
            'ip': ip, 'provider': item.get('isp') or item.get('org') or 'Unknown provider',
            'organization': item.get('org') or 'Not reported', 'asn': item.get('asn') or 'Not reported',
            'latitude': lat, 'longitude': lon, 'city': loc.get('city'), 'country': loc.get('country_name'),
            'ports': [], 'products': [], 'hostnames': [], 'last_seen': item.get('timestamp'),
        })
        for port in [item.get('port')] + (item.get('ports') or []):
            if isinstance(port, int) and 0 <= port <= 65535 and port not in row['ports']:
                row['ports'].append(port)
        for name in item.get('hostnames') or []:
            if isinstance(name, str) and name not in row['hostnames']:
                row['hostnames'].append(name[:240])
        for product in [item.get('product')]:
            if isinstance(product, str) and product not in row['products']:
                row['products'].append(product[:160])
        if (item.get('timestamp') or '') > (row['last_seen'] or ''):
            row['last_seen'] = item.get('timestamp')
    for row in hosts.values():
        row['ports'].sort()
    return list(hosts.values())


def saved_pages():
    """Read the existing v0.1 cache directly; refresh only changed files."""
    with LOCK:
        files = sorted((STATE / 'areas').glob('*.json'))
        signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
        if CATALOG['signature'] != signature:
            pages = []
            for path in files:
                try:
                    data = json.loads(path.read_text())
                    data['bounds'] = validate_bounds(data['bounds'])
                    if type(data['page']) is not int or not 1 <= data['page'] <= 10:
                        continue
                    if not isinstance(data['hosts'], list):
                        continue
                    datetime.fromisoformat(data['retrieved_at']).timestamp()
                    pages.append(data)
                except (OSError, ValueError, TypeError, KeyError, AppError):
                    continue
            CATALOG.update(signature=signature, pages=pages, hosts=merge_saved_hosts(pages))
        return CATALOG['pages']


def age_seconds(page):
    return max(0, time.time() - datetime.fromisoformat(page['retrieved_at']).timestamp())


def rectangle_area(bounds):
    s, w, n, e = bounds
    return (n - s) * (e - w)


def intersection(a, b):
    s, w, n, e = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return (s, w, n, e) if s < n and w < e else None


def sampled_fraction(bounds, rectangles):
    """Union of search footprints, not an estimate of inventory completeness."""
    clips = [clip for box in rectangles if (clip := intersection(bounds, box))]
    edges = sorted({x for box in clips for x in (box[1], box[3])})
    covered = 0
    for left, right in zip(edges, edges[1:]):
        intervals = sorted((s, n) for s, w, n, e in clips if w < right and e > left)
        bottom = top = None
        height = 0
        for s, n in intervals:
            if top is None or s > top:
                if top is not None:
                    height += top - bottom
                bottom, top = s, n
            else:
                top = max(top, n)
        if top is not None:
            height += top - bottom
        covered += (right - left) * height
    return min(1, covered / rectangle_area(bounds))


def plan_area(bounds, pages=None):
    bounds = validate_bounds(bounds)
    pages = saved_pages() if pages is None else pages
    candidates = []
    for item in pages:
        if item['page'] != 1:
            continue
        clip = intersection(bounds, item['bounds'])
        overlap = rectangle_area(clip) / rectangle_area(bounds) if clip else 0
        saved_overlap = rectangle_area(clip) / rectangle_area(item['bounds']) if clip else 0
        # Reuse a nearby sample after small pans, but don't substitute a huge
        # region's first 100 records for a neighborhood search.
        if (overlap >= .8 or (overlap >= .2 and saved_overlap >= .8)) and rectangle_area(item['bounds']) <= 4 * rectangle_area(bounds):
            similarity = rectangle_area(clip) / (rectangle_area(bounds) + rectangle_area(item['bounds']) - rectangle_area(clip))
            candidates.append((similarity, -rectangle_area(item['bounds']), item['retrieved_at'], item))
    chosen = max(candidates, key=lambda x: x[:3])[3] if candidates else None
    target = chosen['bounds'] if chosen else bounds
    loaded = sorted((x for x in pages if x['bounds'] == target), key=lambda x: x['page'])
    page_numbers = {x['page'] for x in loaded}
    next_page = next((p for p in range(1, 11) if p not in page_numbers), None)
    total = int(chosen.get('total_records', 0)) if chosen else 0
    stale = age_seconds(chosen) >= CACHE_TTL if chosen else False
    if chosen and (stale or next_page is None or (next_page - 1) * 100 >= total):
        next_page = None
    return {'bounds': target, 'cached': bool(chosen), 'credits': 0 if chosen else 1,
            'pages': sorted(page_numbers), 'next_page': next_page,
            'total_records': total, 'loaded_records': sum(x.get('record_count', 0) for x in loaded),
            'retrieved_at': chosen['retrieved_at'] if chosen else None,
            'stale': stale,
            'view_fraction': round(sampled_fraction(bounds, [target]), 4) if chosen else 0}


def merge_saved_hosts(pages):
    hosts = {}
    # Older observations first: the newest collected snapshot owns an IP's
    # position. Filter after merging so a moved IP cannot leave a ghost marker.
    for page in sorted(pages, key=lambda p: p['retrieved_at']):
        for host in page['hosts']:
            ip = host.get('ip')
            if not ip:
                continue
            previous = hosts.get(ip)
            if previous:
                host = {**host, **{field: sorted(set(previous.get(field, []) + host.get(field, [])))
                                  for field in ('ports', 'products', 'hostnames')}}
            hosts[ip] = {**host, 'saved_at': page['retrieved_at']}
    return list(hosts.values())


def saved_view(bounds):
    bounds = validate_bounds(bounds, search=False)
    with LOCK:
        pages = saved_pages()
        hosts = CATALOG['hosts']
    overlapping = [p for p in pages if intersection(bounds, p['bounds'])]
    visible = []
    for host in hosts:
        lat, lon = host.get('latitude'), host.get('longitude')
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and bounds[0] <= lat <= bounds[2] and bounds[1] <= lon <= bounds[3]:
            visible.append(host)
    dates = [p['retrieved_at'] for p in overlapping]
    footprints = {p['bounds'] for p in overlapping}
    try:
        plan = plan_area(bounds, pages)
    except AppError as exc:
        plan = {'error': exc.message, 'credits': None, 'cached': False}
    return {'hosts': visible, 'bounds': bounds, 'known_hosts': len(hosts),
            'area_count': len(footprints), 'page_count': len(overlapping),
            'record_count': sum(p.get('record_count', 0) for p in overlapping),
            'sampled_fraction': round(sampled_fraction(bounds, footprints), 4),
            'oldest_at': min(dates) if dates else None, 'newest_at': max(dates) if dates else None,
            'stale_pages': sum(age_seconds(p) >= CACHE_TTL for p in overlapping),
            'footprints': list(footprints), 'plan': plan}


def area(bounds, page=1, refresh=False, expected_credits=None, expected_snapshot=None):
    bounds = validate_bounds(bounds)
    if type(page) is not int or not 1 <= page <= 10:
        raise AppError('Atlas supports the first ten result pages per area.')
    if refresh and page != 1:
        raise AppError('Refresh an area from its first page.')
    query = query_for(bounds)
    path = cache_path(query, page)
    # Serialize paid requests so repeated clicks cannot consume duplicate credits.
    with LOCK:
        if refresh and expected_snapshot is not None and path.exists():
            try:
                current = json.loads(path.read_text())
                if current['retrieved_at'] != expected_snapshot:
                    current.update(cached=True, stale=age_seconds(current) >= CACHE_TTL, credits_used=0)
                    return current
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if path.exists() and not refresh:
            try:
                result = json.loads(path.read_text())
                result['cached'] = True
                result['stale'] = age_seconds(result) >= CACHE_TTL
                result['credits_used'] = 0
                return result
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if expected_credits == 0:
            raise AppError('The saved result changed. Review the updated credit cost and try again.', 409)
        if page > 1:
            first = next((p for p in saved_pages() if p['bounds'] == bounds and p['page'] == 1), None)
            if first is None or age_seconds(first) >= CACHE_TTL:
                raise AppError('Refresh this saved area before loading more pages.', 409)
            if any(not cache_path(query, earlier).exists() for earlier in range(1, page)):
                raise AppError('Load the preceding result pages first.', 409)
        response = shodan('shodan/host/search', query=query, page=page, minify='true')
        result = {'hosts': normalize(response.get('matches') or []), 'total_records': response.get('total', 0),
                  'page': page, 'bounds': bounds, 'retrieved_at': now(), 'cached': False,
                  'record_count': len(response.get('matches') or []), 'stale': False, 'credits_used': 1}
        atomic_json(path, result)
        if refresh:
            # Pagination belongs to one snapshot; don't silently combine a new
            # first page with old later pages from a different search ordering.
            for later in range(2, 11):
                cache_path(query, later).unlink(missing_ok=True)
        CATALOG['signature'] = None
        atomic_json(STATE / 'last-area.json', result)
        if page == 1 and bounds == validate_bounds([DEFAULT_PLACE['bounds'][0][0], DEFAULT_PLACE['bounds'][0][1], DEFAULT_PLACE['bounds'][1][0], DEFAULT_PLACE['bounds'][1][1]]):
            atomic_json(STATE / 'coral-springs.json', result)
        ACCOUNT['at'] = 0
        return result


def initial_data():
    path = STATE / 'coral-springs.json'
    if path.exists():
        try:
            result = json.loads(path.read_text())
            result['cached'] = True
            return result
        except (OSError, ValueError):
            pass
    return None


def geocode(text):
    global LAST_GEOCODE
    if not isinstance(text, str) or not 2 <= len(text.strip()) <= 150:
        raise AppError('Enter a city, region, or country.')
    key = hashlib.sha256(text.strip().lower().encode()).hexdigest()[:20]
    path = STATE / 'places' / (key + '.json')
    if path.exists() and time.time() - path.stat().st_mtime < 30 * 86400:
        return json.loads(path.read_text())
    with GEOCODER_LOCK:
        time.sleep(max(0, 1.05 - (time.monotonic() - LAST_GEOCODE)))
        data = remote_json('https://nominatim.openstreetmap.org/search?' + urlencode({
            'q': text.strip(), 'format': 'jsonv2', 'limit': 5, 'accept-language': 'en'}))
        LAST_GEOCODE = time.monotonic()
    results = [{'name': x.get('display_name', ''), 'latitude': float(x['lat']), 'longitude': float(x['lon']),
                'bounds': [float(v) for v in x['boundingbox']]} for x in data]
    atomic_json(path, results)
    return results


def host_detail(ip):
    try:
        address = ipaddress.ip_address(ip)
        if address.version != 4 or not address.is_global:
            raise ValueError
    except ValueError:
        raise AppError('Select a public IPv4 address.') from None
    path = STATE / 'hosts' / (str(address) + '.json')
    if path.exists() and time.time() - path.stat().st_mtime < 3600:
        return json.loads(path.read_text())
    data = shodan('shodan/host/' + str(address), minify='true')
    loc = {key: data.get(key) for key in ['latitude', 'longitude', 'city', 'country_name']}
    rows = normalize([{**data, 'location': loc, 'timestamp': data.get('last_update')}])
    row = rows[0] if rows else {'ip': str(address)}
    row['comparison'] = None
    try:
        extra = remote_json('https://ipinfo.io/' + str(address) + '/json')
        row['comparison'] = {key: extra.get(key) for key in ['city', 'region', 'country', 'loc', 'anycast', 'org']}
    except AppError:
        row['comparison_unavailable'] = True
    atomic_json(path, row)
    return row


class Handler(BaseHTTPRequestHandler):
    server_version = 'FoxtailAtlas/0.2'

    def log_message(self, *_):
        pass  # Do not log authenticated upstream URLs or user searches.

    def headers_common(self, content_type='application/json; charset=utf-8', cache='no-store'):
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://tile.openstreetmap.org https://*.basemaps.cartocdn.com https://server.arcgisonline.com; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'")

    def safe_host(self):
        host = self.headers.get('Host', '')
        return host in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

    def write_json(self, value, status=200):
        blob = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.headers_common()
        self.send_header('Content-Length', str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        if not self.safe_host():
            self.write_json({'error': 'Host not allowed.'}, 403)
            return
        route = urlsplit(self.path)
        try:
            if route.path == '/api/health':
                self.write_json({'ok': True, 'version': VERSION})
            elif route.path == '/api/bootstrap':
                self.write_json({'version': VERSION, 'place': DEFAULT_PLACE, 'account': account()})
            elif route.path == '/api/view':
                self.write_json(saved_view(json.loads(parse_qs(route.query).get('bounds', ['null'])[0])))
            elif route.path == '/api/places':
                self.write_json({'places': geocode(parse_qs(route.query).get('q', [''])[0])})
            elif route.path.startswith('/api/host/'):
                self.write_json(host_detail(route.path.split('/')[-1]))
            elif route.path in {'/', '/index.html'}:
                source = (ROOT / 'static/index.html').read_text().replace('__ATLAS_TOKEN__', TOKEN)
                self.send_blob(source.encode(), 'text/html; charset=utf-8')
            elif route.path.startswith('/static/'):
                name = route.path[len('/static/'):]
                file = (ROOT / 'static' / name).resolve()
                if not file.is_relative_to((ROOT / 'static').resolve()) or not file.is_file():
                    raise AppError('File not found.', 404)
                mime = {'.css': 'text/css', '.js': 'application/javascript', '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon'}.get(file.suffix, 'text/plain')
                cache = 'no-cache' if name in {'app.js', 'style.css'} else 'public, max-age=3600'
                self.send_blob(file.read_bytes(), mime, cache)
            else:
                raise AppError('Not found.', 404)
        except (ValueError, TypeError):
            self.write_json({'error': 'Choose a valid map area.'}, 400)
        except AppError as exc:
            self.write_json({'error': exc.message}, exc.status)
        except Exception:
            self.write_json({'error': 'Atlas could not complete this request. Please try again.'}, 500)

    def send_blob(self, blob, mime, cache='no-store'):
        self.send_response(200)
        self.headers_common(mime, cache)
        self.send_header('Content-Length', str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_POST(self):
        allowed_origins = {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}
        if not self.safe_host() or self.headers.get('Origin') not in allowed_origins or not secrets.compare_digest(self.headers.get('X-Atlas-Token', ''), TOKEN):
            self.write_json({'error': 'This request must come from your Atlas window.'}, 403)
            return
        try:
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise AppError('A JSON request is required.', 415)
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size < 4096:
                raise AppError('Request is too large.', 413)
            data = json.loads(self.rfile.read(size))
            if urlsplit(self.path).path == '/api/area':
                if type(data.get('expected_credits')) is not int or data['expected_credits'] not in (0, 1):
                    raise AppError('Review the query credit cost before loading this area.', 409)
                if type(data.get('refresh', False)) is not bool:
                    raise AppError('Invalid refresh option.')
                self.write_json(area(data.get('bounds'), data.get('page', 1),
                                     data.get('refresh', False), data['expected_credits'], data.get('expected_snapshot')))
            else:
                raise AppError('Not found.', 404)
        except AppError as exc:
            self.write_json({'error': exc.message}, exc.status)
        except (ValueError, TypeError, AttributeError):
            self.write_json({'error': 'Invalid request.'}, 400)
        except Exception:
            self.write_json({'error': 'Atlas could not load this area. Please try again.'}, 500)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18864)
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Foxtail Atlas {VERSION} · http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
