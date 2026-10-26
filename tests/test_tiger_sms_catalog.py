import json
import unittest
from unittest.mock import Mock, patch

from core import sms_provider as sms
from config import codex
from webui.app import create_app


class TigerCatalogTests(unittest.TestCase):
    def query(self, service, responses):
        http = Mock()
        http.get.side_effect = [Mock(status_code=200, text=json.dumps(data)) for data in responses]
        with patch.object(codex, 'TIGER_SMS_API_KEY', 'test-key'):
            result = sms.get_tiger_catalog(service, http=http)
        return result, http

    def test_services(self):
        result, http = self.query(None, [{'status': 'success', 'services': [
            {'code': 'dr', 'name': 'OpenAI'}, {'code': 'tg', 'name': 'Telegram'}]}])
        self.assertEqual(result['services'][0], {'code': 'dr', 'name': 'OpenAI'})
        self.assertEqual(http.get.call_args.kwargs['params']['action'], 'getServicesList')
        http.close.assert_not_called()

    def test_country_prices_are_project_specific_and_numeric_sorted(self):
        countries = [{'id': 0, 'chn': '俄罗斯'}, {'id': 6, 'chn': '美国'}, {'id': 19, 'eng': 'Brazil'}]
        prices = {'data': {'dr': {
            '6': {'prices': {'default': '10'}, 'counts': {'total': 10}},
            '19': {'prices': {'default': '2'}, 'counts': {'total': 8}},
            '0': {'prices': {'default': '0.1'}, 'counts': {'total': 0}},
            '99': {'prices': {'default': 'NaN'}, 'counts': {'total': 8}},
            '100': {'prices': {'default': '-1'}, 'counts': {'total': 8}},
        }, 'tg': {'10': {'prices': {'default': '0.01'}, 'counts': {'total': 99}}}}}
        result, http = self.query('ChatGPT', [countries, prices])
        self.assertEqual(result['service'], 'dr')
        self.assertEqual(result['countries'], [
            {'id': '19', 'name': 'Brazil', 'price': 2.0, 'count': 8, 'buckets': []},
            {'id': '6', 'name': '美国', 'price': 10.0, 'count': 10, 'buckets': []}])
        actions = [c.kwargs['params']['action'] for c in http.get.call_args_list]
        self.assertEqual(actions, ['getCountries', 'getOffers'])
        self.assertEqual(http.get.call_args.kwargs['params']['services'], 'dr')

    def test_country_zero_id_and_dict_catalog_supported(self):
        result, _ = self.query('dr', [{'0': {'id': 0, 'chn': '俄罗斯'}},
                                    {'data': {'dr': {'0': {'prices': {'default': '0'}, 'counts': {'total': 1}}}}}])
        self.assertEqual(result['countries'][0]['id'], '0')
        self.assertEqual(result['countries'][0]['name'], '俄罗斯')

    def test_price_buckets_preserve_individual_stock_and_precision(self):
        offer = {'prices': {'default': '0.30'}, 'counts': {'total': 35},
                 'map': {'0.2500': 15, '0.1500': 20, '0.1000': 0,
                         'NaN': 10, '-1': 1, '0': 1, 'oops': 1, '0.5': 'bad'}}
        result, _ = self.query('dr', [[{'id': 6, 'chn': '美国'}],
                                    {'data': {'dr': {'6': offer}}}])
        country = result['countries'][0]
        self.assertEqual(country['count'], 35)
        self.assertEqual(country['price'], 0.30)
        self.assertEqual(country['buckets'], [
            {'price': '0.1500', 'count': 20}, {'price': '0.2500', 'count': 15}])

    def test_country_sort_uses_lowest_in_stock_bucket(self):
        result, _ = self.query('dr', [[], {'data': {'dr': {
            '6': {'prices': {'default': 10}, 'counts': {'total': 5}, 'map': {'0.1500': 5}},
            '19': {'prices': {'default': 2}, 'counts': {'total': 3}, 'map': {'0.2000': 3}},
        }}}])
        self.assertEqual([country['id'] for country in result['countries']], ['6', '19'])

    def test_empty_inventory(self):
        result, _ = self.query('dr', [[], {'data': {}}])
        self.assertEqual(result['countries'], [])

    def test_bad_responses(self):
        for service, responses in [(None, [[]]), (None, [{'title': 'BAD_KEY'}]),
                                   ('dr', [False]), ('dr', [[], []])]:
            with self.subTest(service=service, responses=responses):
                with self.assertRaises(sms.SmsProviderError):
                    self.query(service, responses)

    def test_owned_session_closed(self):
        http = Mock()
        http.get.return_value = Mock(status_code=200, text='not-json')
        with patch.object(codex, 'TIGER_SMS_API_KEY', 'test-key'), patch.object(sms, '_http', return_value=http):
            with self.assertRaises(sms.SmsProviderError):
                sms.get_tiger_catalog()
        http.close.assert_called_once()


class TigerCatalogRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code='test-auth').test_client()
        self.client.environ_base['HTTP_X_AUTH_CODE'] = 'test-auth'

    def test_route_passes_selected_project(self):
        with patch.object(sms, 'get_tiger_catalog', return_value={'countries': [], 'service': 'dr'}) as catalog:
            response = self.client.get('/api/sms/tiger/catalog?service=dr')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['ok'])
        catalog.assert_called_once_with(service='dr')

    def test_route_reports_errors_without_leaking_network_details(self):
        for exc, status in [(sms.SmsProviderError('Tiger SMS API Key 不能为空'), 400),
                            (RuntimeError('secret-url'), 502)]:
            with patch.object(sms, 'get_tiger_catalog', side_effect=exc):
                response = self.client.get('/api/sms/tiger/catalog')
            self.assertEqual(response.status_code, status)
            self.assertFalse(response.json['ok'])
            self.assertNotIn('secret-url', str(response.json))

    def test_both_pages_render_shared_catalog(self):
        for path in ('/?ui=modern', '/?ui=legacy'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'function sortedTigerCountries', response.data)
            self.assertIn(b'renderTigerCatalogTools()', response.data)
