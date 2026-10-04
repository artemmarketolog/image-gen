import base64
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compress
import generate
import batch_generate
from PIL import Image

USAGE = {'input_tokens': 46, 'input_tokens_details': {'image_tokens': 0, 'text_tokens': 46},
         'output_tokens': 1413, 'output_tokens_details': {'image_tokens': 1413, 'text_tokens': 0}}


def resp(status, body=None, text=''):
    r = Mock(status_code=status, text=text or str(body))
    r.json.return_value = body or {}
    return r


class OpenAIProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        im = Image.new('RGB', (64, 64), 'pink'); b = io.BytesIO(); im.save(b, 'PNG')
        self.ok = resp(200, {'data': [{'b64_json': base64.b64encode(b.getvalue()).decode()}], 'usage': USAGE})
        self.patches = [patch.object(compress, 'STATE_DIR', self.root), patch.object(generate, 'API_KEY', 'lz'),
                        patch.object(generate, 'OPENAI_API_KEY', 'oa'), patch.object(generate, 'log_started'),
                        patch.object(generate, 'log_failed'), patch.object(generate, 'log_prompt_md'),
                        patch.object(generate.time, 'sleep')]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)
        billed = patch.object(generate, 'log_billed'); self.billed = billed.start(); self.addCleanup(billed.stop)

    def test_openai_request_and_actual_cost(self):
        with patch.object(generate.requests, 'post', return_value=self.ok) as post:
            generate.generate_image('x', self.root / 'a.png', provider='openai', aspect_ratio='9:16',
                                    job_id='j1', moderation='low', background='transparent')
        url, kw = post.call_args.args[0], post.call_args.kwargs
        self.assertEqual(url, 'https://api.openai.com/v1/images/generations')
        self.assertEqual(kw['headers']['Authorization'], 'Bearer oa')
        self.assertEqual(kw['json'], {'model': 'gpt-image-2.5-flare', 'prompt': 'x', 'size': '1152x2048',
                                      'quality': 'medium', 'background': 'transparent', 'output_format': 'png',
                                      'moderation': 'low'})
        self.assertAlmostEqual(generate.ACTUAL_COSTS['j1'], 0.04262, places=5)
        self.billed.assert_called_once()

    def test_openai_square_is_1024(self):
        self.assertEqual(generate.resolve_gpt_size('2K', '1:1', 'openai'), '1024x1024')
        self.assertEqual(generate.resolve_gpt_size('2K', '1:1'), '2048x2048')
        self.assertEqual(generate.resolve_gpt_size('2048x2048', '1:1', 'openai'), '2048x2048')

    def test_laozhang_down_falls_back_to_openai(self):
        down = resp(500, {'error': {'code': 'do_request_failed'}}, text='do_request_failed')
        busy = resp(503, {'error': {'code': 'model_service_unavailable'}})
        with patch.object(generate.requests, 'post', side_effect=[down, busy, busy, busy, self.ok]) as post:
            generate.generate_image('x', self.root / 'b.png', job_id='j2')
        urls = [c.args[0] for c in post.call_args_list]
        self.assertTrue(all('laozhang' in u for u in urls[:4]))
        self.assertEqual(urls[4], 'https://api.openai.com/v1/images/generations')
        self.assertEqual(post.call_args.kwargs['json']['model'], 'gpt-image-2.5-flare')
        self.assertEqual(post.call_args.kwargs['json']['quality'], 'medium')

    def test_old_model_never_falls_back(self):
        busy = resp(503, {'error': {'code': 'model_service_unavailable'}})
        with patch.object(generate.requests, 'post', return_value=busy) as post:
            with self.assertRaises(generate.NotBilledError):
                generate.generate_image('x', self.root / 'c.png', model='gpt-image-2')
        self.assertEqual(post.call_count, 3)

    def test_insufficient_quota_is_not_retried(self):
        quota = resp(429, {'error': {'code': 'insufficient_quota', 'message': 'quota'}})
        with patch.object(generate.requests, 'post', return_value=quota) as post:
            with self.assertRaises(generate.NotBilledError):
                generate.generate_image('x', self.root / 'd.png', provider='openai')
        self.assertEqual(post.call_count, 1)

    def test_batch_reports_refusals_as_not_billed(self):
        # laozhang 503 без ключа OpenAI: переходить некуда, но и не списано
        busy = resp(503, {'error': {'code': 'model_service_unavailable'}})
        with patch.object(generate.requests, 'post', return_value=busy), \
                patch.object(generate, 'OPENAI_API_KEY', None):
            r2 = batch_generate.run_one_job({'prompt': 'x', 'name': 'n'}, 't', 1)
        self.assertEqual(r2['status'], 'failed')
        blocked = resp(400, {'error': {'code': 'moderation_blocked', 'message': 'blocked',
                                       'moderation_details': {'moderation_stage': 'input'}}})
        with patch.object(generate.requests, 'post', return_value=blocked):
            r3 = batch_generate.run_one_job({'prompt': 'x', 'name': 'o', 'provider': 'openai'}, 't', 2)
        self.assertEqual(r3['status'], 'failed')
        self.assertIn('moderation', r3['error'])

    def test_batch_uses_actual_cost(self):
        with patch.object(generate.requests, 'post', return_value=self.ok), \
                patch.object(batch_generate, 'log_success'), patch.object(batch_generate, 'REFS_DIR', self.root):
            r = batch_generate.run_one_job({'prompt': 'x', 'name': 'p', 'provider': 'openai', 'quality': 'high'}, 't', 3)
        self.assertEqual(r['status'], 'success')
        self.assertAlmostEqual(r['cost'], 0.04262, places=5)

    def test_cost_estimates(self):
        self.assertEqual(generate.get_cost('gpt-image-2.5'), 0.03)
        self.assertEqual(generate.get_cost('gpt-image-2.5', 'openai', 'medium'), 0.011)
        self.assertAlmostEqual(generate.get_cost('gpt-image-2.5', 'openai', None, refs=2), 0.035)
        # квадрат 2K на high: замер $0.107, оценка через площадь^1.5
        self.assertAlmostEqual(generate.get_cost('gpt-image-2.5', 'openai', 'high', size='2048x2048'), 0.102, places=3)
        self.assertEqual(generate.get_cost('gpt-image-2.5', 'openai', 'high', size='1024x1024'), 0.043)
        with_refs = dict(USAGE, input_tokens_details={'image_tokens': 1508, 'text_tokens': 35})
        self.assertAlmostEqual(generate.openai_cost(with_refs), 0.054629, places=6)


if __name__ == '__main__':
    unittest.main()
