import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


class ReleaseTests(unittest.TestCase):
    def test_standalone_release_is_create_only_and_does_not_publish_stackset_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = root / 'aws-call.json'
            aws = root / 'aws'
            aws.write_text('#!/usr/bin/env python3\nimport json,os,sys\nopen(os.environ["CALL_LOG"],"w").write(json.dumps(sys.argv[1:]))\nprint("{}")\n')
            aws.chmod(0o700)
            env = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'], 'CALL_LOG': str(log)}
            result = subprocess.run(['bash', 'release.sh', 'test-wif-artifacts', '--yes'], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(log.read_text())
            self.assertEqual(args[:2], ['s3api', 'put-object'])
            self.assertEqual(args[args.index('--if-none-match') + 1], '*')
            self.assertEqual(args[args.index('--key') + 1], 'aws-terraform-wif/v1.0.0/terraform_wif_bootstrap.py')
            self.assertEqual(args[args.index('--body') + 1], 'terraform_wif_bootstrap.py')
            self.assertNotIn('main_organizations', ' '.join(args))
            self.assertIn(hashlib.sha256((ROOT / 'terraform_wif_bootstrap.py').read_bytes()).hexdigest(), result.stdout)


if __name__ == '__main__':
    unittest.main()
