import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server


class AtlasTests(unittest.TestCase):
    def test_geographic_query_uses_northwest_then_southeast(self):
        self.assertEqual(server.query_for([26.2, -80.3, 26.4, -80.1]), 'geo:26.4,-80.3,26.2,-80.1')
        for bounds in [None, [0, 0, float('nan'), 1], [0, 0, 20, 20], [30, -80, 20, -79]]:
            with self.assertRaises(server.AppError):
                server.validate_bounds(bounds)

    def test_services_become_unique_public_ipv4_records(self):
        source = {'ip_str': '8.8.8.8', 'isp': 'Example provider', 'location': {'latitude': 26.27, 'longitude': -80.27}, 'timestamp': None}
        result = server.normalize([{**source, 'port': 53}, {**source, 'port': 443}, {**source, 'ip_str': '192.168.1.1'}, {**source, 'ip_str': '2001:4860::8888'}])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['ports'], [53, 443])

    def test_missing_locations_are_not_fabricated(self):
        result = server.normalize([{'ip_str': '8.8.8.8', 'location': {'latitude': 1000, 'longitude': 0}, 'timestamp': '2026-10-25'}])
        self.assertIsNone(result[0]['latitude'])
        self.assertIsNone(result[0]['longitude'])

    def test_repeated_area_load_reuses_one_paid_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(server, 'STATE', Path(temporary)), patch.object(server, 'shodan', return_value={'total': 0, 'matches': []}) as upstream:
                first = server.area([26.2, -80.3, 26.4, -80.1])
                second = server.area([26.2, -80.3, 26.4, -80.1])
                self.assertFalse(first['cached'])
                self.assertTrue(second['cached'])
                upstream.assert_called_once()

    def save_page(self, bounds, page=1, age=0, hosts=None, total=1000, records=100):
        bounds = server.validate_bounds(bounds)
        data = {'bounds': bounds, 'page': page, 'retrieved_at': datetime.fromtimestamp(time.time()-age, timezone.utc).isoformat(),
                'hosts': hosts or [], 'total_records': total, 'record_count': records}
        server.atomic_json(server.cache_path(server.query_for(bounds), page), data)
        return data

    def test_small_pan_reuses_saved_geographic_area_without_upstream(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)), patch.object(server, 'shodan') as upstream:
            box = [26.2, -80.3, 26.4, -80.1]
            self.save_page(box)
            plan = server.saved_view([26.21, -80.29, 26.41, -80.09])['plan']
            self.assertTrue(plan['cached'])
            self.assertEqual(plan['bounds'], tuple(box))
            self.assertEqual(plan['credits'], 0)
            self.assertEqual(plan['next_page'], 2)
            self.assertLess(plan['view_fraction'], 1)
            upstream.assert_not_called()

    def test_stale_results_survive_restart_and_never_refresh_automatically(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)), patch.object(server, 'shodan') as upstream:
            box = [26.2, -80.3, 26.4, -80.1]
            self.save_page(box, age=90000, hosts=[{'ip':'8.8.8.8', 'latitude':26.3, 'longitude':-80.2}])
            server.CATALOG['signature'] = None
            view = server.saved_view(box)
            self.assertEqual(len(view['hosts']), 1)
            self.assertEqual(view['stale_pages'], 1)
            self.assertTrue(view['plan']['stale'])
            self.assertIsNone(view['plan']['next_page'])
            cached = server.area(box, expected_credits=0)
            self.assertTrue(cached['cached'])
            self.assertTrue(cached['stale'])
            upstream.assert_not_called()
            with self.assertRaises(server.AppError):
                server.area(box, page=2, expected_credits=1)
            upstream.assert_not_called()

    def test_all_saved_areas_remain_available_and_newest_location_wins(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)), patch.object(server, 'shodan') as upstream:
            a, b = [26.2,-80.3,26.4,-80.1], [27.2,-81.3,27.4,-81.1]
            self.save_page(a, age=500, hosts=[{'ip':'8.8.8.8','latitude':26.3,'longitude':-80.2,'ports':[53]}, {'ip':'1.1.1.1','latitude':26.3,'longitude':-80.2}])
            self.save_page(b, hosts=[{'ip':'8.8.8.8','latitude':27.3,'longitude':-81.2,'ports':[443]}])
            self.assertEqual([h['ip'] for h in server.saved_view(a)['hosts']], ['1.1.1.1'])
            moved = server.saved_view(b)['hosts'][0]
            self.assertEqual(moved['ports'], [53,443])
            self.assertEqual(server.saved_view(a)['known_hosts'], 2)
            upstream.assert_not_called()

    def test_union_of_search_footprints_does_not_double_count_overlap(self):
        self.assertAlmostEqual(server.sampled_fraction([0,0,2,2], [[0,0,1,2],[0,0,2,1]]), .75)
        self.assertEqual(server.sampled_fraction([0,0,2,2], [[0,0,2,2],[0,0,2,2]]), 1)
        self.assertEqual(server.sampled_fraction([0,0,2,2], [[3,3,4,4]]), 0)

    def test_neighborhood_does_not_reuse_a_huge_region_sample(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)):
            self.save_page([25,-81,27,-79])
            plan = server.plan_area([26.2,-80.3,26.4,-80.1])
            self.assertFalse(plan['cached'])
            self.assertEqual(plan['credits'], 1)

    def test_zoomed_out_saved_browsing_is_free_but_new_search_requires_zoom(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)), patch.object(server, 'shodan') as upstream:
            self.save_page([26.2,-80.3,26.4,-80.1], hosts=[{'ip':'8.8.8.8','latitude':26.3,'longitude':-80.2}])
            view = server.saved_view([-80,-180,80,180])
            self.assertEqual(len(view['hosts']), 1)
            self.assertIn('Zoom in', view['plan']['error'])
            upstream.assert_not_called()

    def test_free_request_cannot_become_paid_if_cache_disappears(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)), patch.object(server, 'shodan') as upstream:
            with self.assertRaises(server.AppError) as error:
                server.area([26.2,-80.3,26.4,-80.1], expected_credits=0)
            self.assertEqual(error.exception.status, 409)
            upstream.assert_not_called()

    def test_refresh_replaces_snapshot_and_resets_only_its_later_pages(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)), patch.object(server, 'shodan', return_value={'total':250,'matches':[]}) as upstream:
            box = [26.2,-80.3,26.4,-80.1]
            for page in [1,2,3]: self.save_page(box, page=page)
            other = self.save_page([27.2,-80.3,27.4,-80.1])
            server.area(box, refresh=True, expected_credits=1)
            plan = server.plan_area(box)
            self.assertEqual(plan['pages'], [1])
            self.assertEqual(plan['next_page'], 2)
            self.assertEqual(plan['total_records'], 250)
            self.assertEqual(len(server.saved_pages()), 2)
            upstream.assert_called_once()

    def test_failed_refresh_keeps_previous_saved_pages(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)), patch.object(server, 'shodan', side_effect=server.AppError('offline',502)):
            box = [26.2,-80.3,26.4,-80.1]
            self.save_page(box)
            self.save_page(box, page=2)
            with self.assertRaises(server.AppError): server.area(box, refresh=True, expected_credits=1)
            self.assertEqual(server.plan_area(box)['pages'], [1,2])

    def test_simultaneous_loads_spend_only_one_credit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)), patch.object(server, 'shodan', return_value={'total':0,'matches':[]}) as upstream:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results=list(pool.map(lambda _:server.area([26.2,-80.3,26.4,-80.1],expected_credits=1), range(4)))
            self.assertEqual(sum(not x['cached'] for x in results), 1)
            upstream.assert_called_once()

    def test_concurrent_refreshes_of_one_snapshot_spend_only_once(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)), patch.object(server, 'shodan', return_value={'total':0,'matches':[]}) as upstream:
            box = [26.2,-80.3,26.4,-80.1]
            old = self.save_page(box, age=90000)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _:server.area(box,refresh=True,expected_credits=1,expected_snapshot=old['retrieved_at']), range(4)))
            self.assertEqual(sum(not x['cached'] for x in results), 1)
            upstream.assert_called_once()

    def test_empty_search_has_a_saved_footprint_and_no_more_pages(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)):
            self.save_page([26.2,-80.3,26.4,-80.1], total=0, records=0)
            view = server.saved_view([26.2,-80.3,26.4,-80.1])
            self.assertTrue(view['plan']['cached'])
            self.assertEqual(view['sampled_fraction'], 1)
            self.assertIsNone(view['plan']['next_page'])

    def test_paid_requests_require_same_origin_and_window_token(self):
        service = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        origin = f'http://127.0.0.1:{service.server_port}'
        payload = json.dumps({'bounds': [26.2, -80.3, 26.4, -80.1], 'expected_credits': 1}).encode()
        try:
            with patch.object(server, 'area', return_value={'hosts': []}) as area:
                for headers in [{'Origin': origin}, {'Origin': 'https://unrelated.example', 'X-Atlas-Token': server.TOKEN}]:
                    req = Request(origin + '/api/area', data=payload, headers={'Content-Type': 'application/json', **headers})
                    with self.assertRaises(HTTPError) as error:
                        urlopen(req)
                    self.assertEqual(error.exception.code, 403)
                area.assert_not_called()
                # A valid origin/token still cannot issue a paid request without
                # explicitly declaring the displayed credit cost.
                req = Request(origin + '/api/area', data=json.dumps({'bounds':[26.2,-80.3,26.4,-80.1]}).encode(), headers={'Content-Type':'application/json','Origin':origin,'X-Atlas-Token':server.TOKEN})
                with self.assertRaises(HTTPError) as error:
                    urlopen(req)
                self.assertEqual(error.exception.code, 409)
                area.assert_not_called()
                with patch.object(server, 'saved_view', return_value={'hosts':[], 'plan':{'credits':0}}) as view:
                    with urlopen(origin + '/api/view?bounds=%5B26.2,-80.3,26.4,-80.1%5D') as response:
                        self.assertEqual(json.load(response)['plan']['credits'], 0)
                    view.assert_called_once()
                    with self.assertRaises(HTTPError) as error:
                        urlopen(origin + '/api/view?bounds=invalid-json')
                    self.assertEqual(error.exception.code, 400)
                req = Request(origin + '/api/area', data=payload, headers={'Content-Type': 'application/json', 'Origin': origin, 'X-Atlas-Token': server.TOKEN})
                with urlopen(req) as result:
                    self.assertEqual(result.status, 200)
                area.assert_called_once()
        finally:
            service.shutdown()
            service.server_close()


if __name__ == '__main__':
    unittest.main()
