import base64
import io
from pathlib import Path
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compress
import generate
import batch_generate
from PIL import Image


class CompressionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = patch.object(compress, 'STATE_DIR', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)
        im = Image.effect_noise((1200, 1200), 30).convert('RGB')
        b = io.BytesIO(); im.save(b, 'PNG'); self.data = b.getvalue()

    def test_jpeg_size_dimensions_and_persistent_project(self):
        p = compress.save_compressed(self.data, 'photo.png', 'client/task', (1080, 1080))
        self.assertEqual(p.suffix, '.jpg')
        self.assertLess(p.stat().st_size, len(self.data))
        with Image.open(p) as image:
            self.assertEqual(image.size, (1080, 1080))
        self.assertTrue(p.is_relative_to(self.root / 'creatives/client/task/images'))
        self.assertEqual(list(p.parent.iterdir()), [p])

    def test_alpha_preserved(self):
        im = Image.new('RGBA', (100, 100), (255, 30, 40, 80))
        b = io.BytesIO(); im.save(b, 'PNG', compress_level=0)
        p = compress.save_compressed(b.getvalue(), 'alpha.png')
        with Image.open(p) as result:
            self.assertEqual(result.convert('RGBA').tobytes(), im.tobytes())

    def test_no_growth_no_input_loss(self):
        im = Image.new('RGB', (10, 10), 'white'); b = io.BytesIO(); im.save(b, 'PNG')
        p = compress.save_compressed(b.getvalue(), 'tiny.png')
        self.assertEqual(p.read_bytes(), b.getvalue())

    def test_encoder_failure_keeps_original(self):
        with patch.object(Image.Image, 'save', side_effect=OSError('encoder failed')):
            p = compress.save_compressed(self.data, 'paid.png')
        self.assertEqual(p.read_bytes(), self.data)

    def test_concurrent_identical_names(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(lambda _: compress.save_compressed(self.data, 'same.png'), range(4)))
        self.assertEqual(len(set(paths)), 4)
        self.assertTrue(all(p.is_file() for p in paths))

    def test_project_validation(self):
        for project in ('../escape', '/absolute'):
            with self.assertRaises(ValueError):
                compress.save_compressed(self.data, 'x.png', project)

    def test_single_and_batch_with_mocked_api(self):
        response = Mock(status_code=200)
        response.json.return_value = {'data': [{'b64_json': base64.b64encode(self.data).decode()}]}
        with patch.object(generate, 'API_KEY', 'test'), patch.object(generate.requests, 'post', return_value=response) as post, patch.object(generate, 'log_started'), patch.object(generate, 'log_prompt_md') as journal, patch.object(batch_generate, 'log_success') as success, patch.object(batch_generate, 'REFS_DIR', self.root):
            p = generate.generate_image('test', self.root / 'one.png', keep_2k=True)
            with Image.open(p) as image:
                self.assertEqual(image.size, (1200, 1200))
            self.assertEqual(journal.call_args.kwargs['output'], str(p))
            job = batch_generate.run_one_job({'prompt':'test', 'name':'batch', 'project':'client/job'}, 'test', 0)
            self.assertEqual(job['status'], 'success')
            with Image.open(job['output']) as image:
                self.assertEqual(image.size, (1080, 1080))
            self.assertEqual(success.call_args.args[1], job['output'])
            self.assertEqual(post.call_count, 2)


if __name__ == '__main__':
    unittest.main()
