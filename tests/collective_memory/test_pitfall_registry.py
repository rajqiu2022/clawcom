import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_module(name: str, rel_path: str):
    module_path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


knowledge_client = _load_module(
    'collective_memory_knowledge_client',
    'tools/collective_memory/knowledge_client.py',
)
pitfall_registry = _load_module(
    'collective_memory_pitfall_registry',
    'tools/collective_memory/pitfall_registry.py',
)

KnowledgeQuery = knowledge_client.KnowledgeQuery
HubKnowledgeClient = knowledge_client.HubKnowledgeClient
KnowledgeClientError = knowledge_client.KnowledgeClientError
validate_seed_file = pitfall_registry.validate_seed_file
pitfall_to_knowledge_payload = pitfall_registry.pitfall_to_knowledge_payload
PitfallRegistry = pitfall_registry.PitfallRegistry
search_local_pitfalls = pitfall_registry.search_local_pitfalls
load_seed_entries = pitfall_registry.load_seed_entries


class PitfallRegistryTest(unittest.TestCase):
    def test_seed_file_passes_validation(self):
        errors = validate_seed_file()
        self.assertEqual(errors, [], msg='\n'.join(errors))

    def test_seed_file_has_at_least_ten_entries(self):
        entries = load_seed_entries()
        self.assertGreaterEqual(len(entries), 10)

    def test_pitfall_to_knowledge_payload_shape(self):
        entry = load_seed_entries()[0]
        payload = pitfall_to_knowledge_payload(entry)
        self.assertTrue(payload['title'].startswith('[踩坑]'))
        self.assertEqual(payload['category'], 'pitfall')
        self.assertEqual(payload['scope'], 'project')
        self.assertIn('pitfall_id', payload['content'])

    def test_search_local_pitfalls_by_hub_keyword(self):
        entries = load_seed_entries()
        matched = search_local_pitfalls(entries, project='RacingGO', keywords=['Hub', 'API'], limit=3)
        self.assertTrue(matched)
        self.assertTrue(any('Hub' in item['title'] or item['module'] == 'hub' for item in matched))

    def test_knowledge_query_dataclass(self):
        query = KnowledgeQuery(project='RacingGO', keywords=['WeCom', '图片'], modules=['wecom'], limit=3)
        self.assertEqual(query.project, 'RacingGO')
        self.assertEqual(query.keywords, ['WeCom', '图片'])
        self.assertEqual(query.limit, 3)

    @patch('collective_memory_knowledge_client.requests.request')
    def test_hub_client_create_entry(self, mock_request):
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 201
        mock_response.content = b'{"id": 99, "title": "x"}'
        mock_response.json.return_value = {'id': 99, 'title': 'x'}
        mock_request.return_value = mock_response

        client = HubKnowledgeClient(base_url='http://hub.test', token='tok')
        result = client.create_entry('pitfall', {'title': 't', 'content': 'c'})
        self.assertEqual(result['id'], 99)
        mock_request.assert_called_once()
        call_kwargs = mock_request.call_args.kwargs
        self.assertEqual(call_kwargs['headers']['Authorization'], 'Bearer tok')

    @patch('collective_memory_knowledge_client.requests.request')
    def test_hub_client_search_raises_structured_error(self, mock_request):
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 404
        mock_response.text = 'not found'
        mock_request.return_value = mock_response

        client = HubKnowledgeClient(base_url='http://hub.test', token='tok')
        query = KnowledgeQuery(project='RacingGO', keywords=['Hub'], modules=[], limit=5)
        with self.assertRaises(KnowledgeClientError) as ctx:
            client.search_entries('pitfall', query)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn('hub.test', ctx.exception.url or '')

    def test_registry_import_seeds_dry_run(self):
        registry = PitfallRegistry(client=MagicMock())
        results = registry.import_seeds_to_hub(dry_run=True)
        self.assertGreaterEqual(len(results), 10)
        self.assertTrue(all(item['dry_run'] for item in results))


if __name__ == '__main__':
    unittest.main()
