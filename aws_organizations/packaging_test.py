"""Render Organizations release templates with real yq and fake AWS commands."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class TestOrganizationsPackaging(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.yq = shutil.which("yq")
        if cls.yq is None:
            raise RuntimeError("Packaging tests require mikefarah/yq v4 on PATH")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.organizations = self.root / "aws_organizations"
        self.organizations.mkdir()
        self.quickstart = self.root / "aws_quickstart"
        self.quickstart.mkdir()
        source = Path(__file__).resolve().parent
        for name in ("main_organizations.yaml", "release.sh", "version.txt"):
            shutil.copy(source / name, self.organizations / name)
        self.policy = self.quickstart / "datadog_agentless_saas_policy.json"
        shutil.copy(source.parent / "aws_quickstart" / self.policy.name, self.policy)
        self.original_template = (self.organizations / "main_organizations.yaml").read_bytes()
        self.commands = self.root / "bin"
        self.commands.mkdir()
        self.environment = {
            **os.environ,
            "PATH": f"{self.commands}{os.pathsep}{os.environ['PATH']}",
            "PACKAGING_TEST_OUTPUT": str(self.root / "uploads"),
        }
        self.write_command(
            "aws",
            "import os, pathlib, shutil, sys\n"
            "args = sys.argv[1:]\n"
            "if args[:2] == ['s3api', 'head-object']:\n"
            "    sys.exit(1)\n"
            "assert args[:2] == ['s3', 'cp'], args\n"
            "output = pathlib.Path(os.environ['PACKAGING_TEST_OUTPUT'])\n"
            "output.mkdir()\n"
            "for template in pathlib.Path(args[2]).rglob('*.yaml'):\n"
            "    shutil.copy(template, output / template.name)\n",
        )

    def write_command(self, name, source):
        path = self.commands / name
        path.write_text(f"#!{sys.executable}\n{source}")
        path.chmod(0o755)

    def run_release(self):
        return subprocess.run(
            ["bash", "release.sh", "test-organizations-bucket", "--private", "--yes"],
            cwd=self.organizations,
            env=self.environment,
            capture_output=True,
            text=True,
        )

    def assert_rendered_template(self, path):
        rendered = path.read_text()
        self.assertNotIn("!Rain::Include", rendered)
        result = subprocess.run(
            [self.yq, "-o=json", ".Resources", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        resources = json.loads(result.stdout)
        policies = [
            resource["Properties"]["PolicyDocument"]
            for resource in resources.values()
            if resource["Type"] == "AWS::IAM::ManagedPolicy"
        ]
        self.assertIn(json.loads(self.policy.read_text()), policies)
        for name, resource in resources.items():
            if resource["Type"] == "AWS::Lambda::Function":
                source = resource["Properties"]["Code"].get("ZipFile")
                if source:
                    compile(source, name, "exec")

    def assert_source_restored(self):
        self.assertEqual(
            (self.organizations / "main_organizations.yaml").read_bytes(),
            self.original_template,
        )
        self.assertFalse((self.organizations / "main_organizations.yaml.bak").exists())

    def test_release_uploads_resolved_policy_and_restores_source(self):
        result = self.run_release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rendered = self.root / "uploads" / "main_organizations.yaml"
        self.assert_rendered_template(rendered)
        self.assertNotIn("<VERSION_PLACEHOLDER>", rendered.read_text())
        self.assertNotIn("<BUCKET_PLACEHOLDER>", rendered.read_text())
        self.assert_source_restored()

    def test_release_render_failure_restores_source_without_uploading(self):
        self.policy.unlink()
        result = self.run_release()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "uploads").exists())
        self.assert_source_restored()


if __name__ == "__main__":
    unittest.main()
