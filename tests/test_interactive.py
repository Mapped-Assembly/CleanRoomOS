"""Exercise the exact interactive bridge arguments through persisted controller actions."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('interactive', ROOT/'scripts/interactive.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class InteractiveTests(unittest.TestCase):
    def test_fixture_conversation_and_human_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            def action(name, success=True, **kw):
                args = bridge.arguments(dict(action=name, **kw))
                args[args.index('--db')+1] = str(Path(tmp)/'run.sqlite')
                result = subprocess.run([sys.executable, '-m', 'cleanroom_os.workflow', *args],
                                        cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, success, result.stderr)
                return json.loads(result.stdout) if success else None
            action('context')
            self.assertEqual(action('propose')['state'], 'blocked')
            action('allow', success=False, revision=1)
            action('context', resolved=True, reason='User supplied Room B access 11:00-11:30')
            action('propose')
            action('allow', revision=2, reason='Explicit user manufacturing approval')
            action('collect')
            action('results', normal=True)
            review = action('review')
            self.assertEqual(review['state'], 'review_ready')
            self.assertEqual(review['package']['counts']['matched'], 6)
            self.assertEqual(action('qa-approve', revision=2, package_revision=1,
                                    reason='Explicit user QA approval')['state'], 'qa_approved')

    def test_no_arbitrary_commands_or_database_paths(self):
        for request in ({'action':'bash'}, {'action':'status','db':'elsewhere.sqlite'},
                        {'action':'allow','revision':'2 --role qa'}, {'action':'context','resolved':'yes'}):
            with self.assertRaises(ValueError):
                bridge.arguments(request)
