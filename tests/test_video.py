import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bg_recolor
import video_generate as vg
import video_loop
import numpy as np
from PIL import Image


class VideoGenerateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, value in {'STATE_DIR': root / 'state', 'PUBLIC_DIR': root / 'public',
                            'PUBLIC_URL': 'https://example.test/_gen/'}.items():
            p = patch.object(vg, name, value); p.start(); self.addCleanup(p.stop)
        for name, value in {'read_key': lambda _: 'test-key', 'reachable': lambda _: True}.items():
            p = patch.object(vg, name, value); p.start(); self.addCleanup(p.stop)
        self.frame = root / 'first.png'
        Image.new('RGB', (64, 64), (255, 87, 21)).save(self.frame)

    def argv(self, name='job-a', model='seedance'):
        return ['create', '--model', model, '--name', name, '--first', str(self.frame), '--last', str(self.frame),
                '--prompt', 'coin turns', '--project', 'client/test']

    def ledger(self):
        return [json.loads(l) for l in (vg.STATE_DIR / 'video-gen/ledger.jsonl').read_text().splitlines()]

    def test_estimates_match_billed_facts(self):
        self.assertEqual(vg.estimate_usd('wan', 5, '720p'), 0.45)
        self.assertEqual(vg.estimate_usd('seedance', 5, '720p'), 0.89)
        self.assertEqual(vg.estimate_usd('seedance', 5, '1080p'), 2.01)

    def test_wan_request(self):
        body, headers = vg.build_request('wan', 'https://a/1.png', 'https://a/2.png', 'p', 'blur', 5, '720p', seed=7)
        self.assertEqual(headers, {'X-DashScope-Async': 'enable'})
        self.assertEqual([m['type'] for m in body['input']['media']], ['first_frame', 'last_frame'])
        self.assertEqual(body['input']['negative_prompt'], 'blur')
        self.assertEqual(body['parameters'], {'resolution': '720P', 'duration': 5, 'prompt_extend': False,
                                              'watermark': False, 'seed': 7})

    def test_seedance_request(self):
        body, headers = vg.build_request('seedance', 'https://a/1.png', 'https://a/2.png', 'turn', 'blur')
        self.assertEqual(headers, {})
        self.assertEqual([c.get('role') for c in body['content']], [None, 'first_frame', 'last_frame'])
        self.assertTrue(body['content'][0]['text'].startswith('The first frame is image 1'))
        self.assertIn('Avoid: blur.', body['content'][0]['text'])
        self.assertEqual((body['ratio'], body['generate_audio'], body['watermark']), ('adaptive', False, False))

    def test_seedance_is_default_model(self):
        out = io.StringIO()
        with patch('sys.stdout', out):
            vg.main(['create', '--name', 'job-d', '--first', str(self.frame), '--prompt', 'p', '--dry-run'])
        self.assertIn('doubao-seedance-2-0-260128', out.getvalue())
        self.assertNotIn('wan2.7', out.getvalue())

    def test_dry_run_sends_nothing(self):
        with patch.object(vg, 'http', side_effect=AssertionError('no HTTP in dry run')):
            self.assertEqual(vg.main(self.argv() + ['--dry-run']), 0)
        self.assertFalse(vg.job_path('job-a').exists())

    def test_success_then_repeat_is_refused(self):
        with patch.object(vg, 'http', return_value={'id': 'cgt-1'}) as http:
            self.assertEqual(vg.main(self.argv()), 0)
            job = json.loads(vg.job_path('job-a').read_text())
            self.assertEqual(job['task_id'], 'cgt-1')
            self.assertTrue(all(Path(t).is_file() for t in job['temp']))
            self.assertTrue(job['request']['content'][1]['image_url']['url'].startswith('https://example.test/_gen/'))
            with self.assertRaises(SystemExit):
                vg.main(self.argv())
            self.assertEqual(http.call_count, 1)
        self.assertEqual([e['event'] for e in self.ledger()], ['started', 'created'])

    def test_pay_per_request_hint_no_job_and_cleanup(self):
        err = HTTPError('u', 503, 'x', {}, io.BytesIO(b'no available channels under billing mode [pay-per-request]'))
        with patch.object(vg, 'http', side_effect=err):
            with self.assertRaises(SystemExit) as ctx:
                vg.main(self.argv(model='wan'))
        self.assertIn('по объёму', str(ctx.exception.code))
        self.assertFalse(vg.job_path('job-a').exists())
        self.assertEqual(list(vg.PUBLIC_DIR.iterdir()), [])
        self.assertEqual([e['event'] for e in self.ledger()], ['started', 'http_error'])

    def test_no_answer_after_send_blocks_retry(self):
        with patch.object(vg, 'http', side_effect=URLError('timeout')):
            self.assertEqual(vg.main(self.argv()), 2)
        job = json.loads(vg.job_path('job-a').read_text())
        self.assertEqual(job['status'], 'unknown_after_send')
        self.assertTrue(job['temp'])
        with patch.object(vg, 'http', side_effect=AssertionError('must not resend')):
            with self.assertRaises(SystemExit):
                vg.main(self.argv())

    def test_poll_downloads_and_cleans_temp(self):
        with patch.object(vg, 'http', return_value={'id': 'cgt-2'}):
            vg.main(self.argv(name='job-b'))
        done = {'status': 'succeeded', 'content': {'video_url': 'https://cdn.test/v.mp4'},
                'usage': {'completion_tokens': 108900}}

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with patch.object(vg, 'http', return_value=done), \
             patch.object(vg.urllib.request, 'urlopen', return_value=Resp(b'MP4')):
            self.assertEqual(vg.main(['poll', '--name', 'job-b']), 0)
        job = json.loads(vg.job_path('job-b').read_text())
        self.assertEqual((job['status'], job['usd'], job['temp']), ('completed', 0.9, []))
        self.assertEqual(Path(job['output']).read_bytes(), b'MP4')
        self.assertTrue(Path(job['output']).is_relative_to(vg.STATE_DIR / 'creatives/client/test/videos/job-b'))
        self.assertEqual(list(vg.PUBLIC_DIR.iterdir()), [])


class ReachableTest(unittest.TestCase):
    def test_sends_browser_user_agent(self):
        seen = {}

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(req, timeout):
            seen['ua'] = req.get_header('User-agent')
            return Resp()

        with patch.object(vg.urllib.request, 'urlopen', side_effect=fake):
            self.assertTrue(vg.reachable('https://media.example.com/x.png'))
        self.assertNotIn('Python-urllib', seen['ua'])


class LoopTest(unittest.TestCase):
    def test_pingpong_has_no_repeated_frames(self):
        order = video_loop.loop_positions(10, 'pingpong')
        self.assertEqual(order, list(range(10)) + list(range(8, 0, -1)))
        self.assertTrue(all(a != b for a, b in zip(order, order[1:] + order[:1])))

    def test_ease_starts_from_rest_and_is_monotonic(self):
        forward = video_loop.loop_positions(20, 'forward', ease=4)
        self.assertEqual(forward[0], 0)
        self.assertLess(forward[1] - forward[0], 0.1)
        self.assertTrue(all(b > a for a, b in zip(forward, forward[1:])))
        self.assertEqual(forward[-1], 19)

    def test_bg_recolor_keeps_enclosed_centre(self):
        image = Image.new('RGB', (200, 200), (254, 114, 1))
        pixels = image.load()
        for x in range(200):
            for y in range(200):
                d = ((x - 100) ** 2 + (y - 100) ** 2) ** 0.5
                if 40 <= d <= 55:
                    pixels[x, y] = (190, 190, 195)
        out, bg_old, _ = bg_recolor.recolor(image, '#ff5715')
        self.assertEqual(out.getpixel((5, 5)), (255, 87, 21))
        self.assertEqual(out.getpixel((100, 100)), (254, 114, 1))
        self.assertEqual(bg_old.round().tolist(), [254, 114, 1])

    def test_video_loop_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            raw = tmp / 'raw.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=128x128:rate=24:duration=1',
                            '-pix_fmt', 'yuv420p', str(raw)], check=True)
            out = io.StringIO()
            with patch('sys.stdout', out):
                self.assertEqual(video_loop.main([str(raw), '--name', 'coin', '--out-dir', str(tmp / 'site'),
                                                  '--size', '128', '--ease', '3', '--bg', '#ff5715']), 0)
            names = sorted(p.name for p in (tmp / 'site').iterdir())
            self.assertEqual(len(names), 3)
            self.assertRegex(' '.join(names), r'coin-av1\.[0-9a-f]{8}\.mp4 coin-h264\.[0-9a-f]{8}\.mp4 coin-poster\.[0-9a-f]{8}\.webp')
            self.assertIn('codecs=av01.0.', out.getvalue())
            self.assertIn('width="128" height="128"', out.getvalue())


if __name__ == '__main__':
    unittest.main()
