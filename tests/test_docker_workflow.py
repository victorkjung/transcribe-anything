"""Offline regression tests for fork-safe container publishing."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/build_docker_image.yml'
SCRIPT = ROOT / '.github/scripts/docker-images.sh'

class DockerWorkflowTests(unittest.TestCase):
    def resolve(self, event='push', username='', password=''):
        self.assertTrue(SCRIPT.exists(), 'registry selection helper must exist')
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'output'
            env = dict(os.environ, GITHUB_EVENT_NAME=event,
                       GITHUB_REPOSITORY='VictorKJung/transcribe-anything',
                       DOCKER_USERNAME=username, DOCKER_PASSWORD=password,
                       GITHUB_OUTPUT=str(output))
            result = subprocess.run(['bash', str(SCRIPT)], env=env, text=True,
                                    capture_output=True, check=True)
            text = output.read_text()
            self.assertNotIn('sentinel-secret-password', result.stdout + result.stderr + text)
            return text

    def test_no_secrets_publishes_only_owned_ghcr_image(self):
        output = self.resolve()
        self.assertIn('ghcr.io/victorkjung/transcribe-anything', output)
        self.assertIn('dockerhub_enabled=false', output)
        self.assertNotIn('niteris/', output)

    def test_partial_credentials_disable_dockerhub(self):
        for user, secret in [('victorkjung', ''), ('', 'sentinel-secret-password')]:
            self.assertIn('dockerhub_enabled=false', self.resolve(username=user, password=secret))

    def test_complete_credentials_enable_owned_dockerhub(self):
        output = self.resolve(username='victorkjung', password='sentinel-secret-password')
        self.assertIn('dockerhub_enabled=true', output)
        self.assertIn('\nvictorkjung/transcribe-anything\n', output)

    def test_pull_request_never_enables_dockerhub(self):
        output = self.resolve(event='pull_request', username='victorkjung', password='sentinel-secret-password')
        self.assertIn('dockerhub_enabled=false', output)
        self.assertNotIn('\nvictorkjung/transcribe-anything\n', output)

    def test_workflow_permissions_and_publish_gates(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        job = workflow['jobs']['build']
        self.assertEqual(job.get('permissions'), {'contents': 'read', 'packages': 'write'})
        steps = {step.get('id', step.get('name')): step for step in job['steps']}
        self.assertIn('registries', steps)
        self.assertEqual(steps['meta']['with']['images'], '${{ steps.registries.outputs.images }}')
        self.assertIn("steps.registries.outputs.dockerhub_enabled == 'true'", steps['Login to DockerHub']['if'])
        self.assertIn("github.event_name != 'pull_request'", steps['Login to GitHub Container Registry']['if'])
        self.assertEqual(steps['Build and push Docker image']['with']['push'], "${{ github.event_name != 'pull_request' }}")
        self.assertNotIn('niteris/transcribe-anything', WORKFLOW.read_text())

if __name__ == '__main__':
    unittest.main()
