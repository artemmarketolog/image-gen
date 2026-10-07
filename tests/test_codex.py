import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compress
import generate
from PIL import Image

FAKE = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
home = Path(os.environ['FAKE_CODEX_HOME'])
(home / 'args.json').write_text(json.dumps(sys.argv[1:]))
if os.environ.get('FAKE_CODEX_FAIL'):
    print(json.dumps({'type': 'turn.failed', 'error': {'message': 'Please log in: 401'}}))
    sys.exit(1)
print(json.dumps({'type': 'thread.started', 'thread_id': 't1'}))
out = home / 'generated_images' / 't1'
out.mkdir(parents=True, exist_ok=True)
from PIL import Image
Image.new('RGB', (941, 1672), 'green').save(out / 'exec-1.png')
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'DONE'}}))
"""


class CodexProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        fake = self.root / 'codex'
        fake.write_text(FAKE.replace('#!/usr/bin/env python3', '#!' + sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        for p in [patch.object(compress, 'STATE_DIR', self.root), patch.object(generate, 'CODEX_BIN', str(fake)),
                  patch.object(generate, 'CODEX_HOME', self.root), patch.object(generate, 'log_started'),
                  patch.object(generate, 'log_failed'), patch.object(generate, 'log_prompt_md'),
                  patch.dict(os.environ, {'FAKE_CODEX_HOME': str(self.root)})]:
            p.start(); self.addCleanup(p.stop)

    def test_codex_image_with_refs_is_free_and_delivered(self):
        ref = self.root / 'ref.png'
        Image.new('RGB', (8, 8), 'red').save(ref)
        out = generate.generate_image('green apple', self.root / 'apple.png', provider='codex',
                                      aspect_ratio='9:16', ref_images=[str(ref)], job_id='c1')
        args = json.loads((self.root / 'args.json').read_text())
        self.assertEqual(args[:2], ['exec', '--skip-git-repo-check'])
        self.assertEqual(args[args.index('-i') + 1], str(ref))
        self.assertIn('Aspect ratio 9:16', args[-1])
        self.assertIn('green apple', args[-1])
        self.assertEqual(generate.ACTUAL_COSTS['c1'], 0.0)
        with Image.open(out) as im:
            self.assertEqual(im.size[1], 1672)

    def test_codex_failure_is_not_billed_and_hints_login(self):
        with patch.dict(os.environ, {'FAKE_CODEX_FAIL': '1'}):
            with self.assertRaises(generate.NotBilledError) as e:
                generate.generate_image('x', self.root / 'x.png', provider='codex', job_id='c2')
        self.assertIn('codex login', str(e.exception))

    def test_codex_cost_and_model(self):
        self.assertEqual(generate.get_cost('', 'codex'), 0.0)
        self.assertEqual(generate.normalize_model('gpt-image-2.5', 'codex'), 'codex-image_gen')


if __name__ == '__main__':
    unittest.main()
