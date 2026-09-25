"""Image-picker checks; run with Agent Zero's framework Python, no model calls."""
import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('rrsi_images_tested', Path(__file__).parents[1] / 'api/images.py')
images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(images)
A, B, C = ('sha256:' + c * 64 for c in 'abc')


def result(stdout='', code=0):
    return SimpleNamespace(returncode=code, stdout=stdout)


def metadata(ident, tags, os='linux', **extra):
    return json.dumps(dict(id=ident, tags=tags, os=os, architecture='arm64', size_bytes=1000000000, **extra))


class ImagePickerTests(unittest.TestCase):
    def test_metadata_deduplicates_and_excludes_secrets(self):
        rows = images.image_rows('\n'.join([
            metadata(A, ['registry:5000/agent-zero:dev', '<none>:<none>'], env=['SECRET=value']),
            metadata(B, None, os='windows'), metadata(A, ['registry:5000/agent-zero:dev']),
        ]), None)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]['agent_zero'])
        self.assertEqual(rows[0]['tags'], ['registry:5000/agent-zero:dev'])
        self.assertFalse(rows[1]['selectable'])
        self.assertEqual(rows[1]['name'], 'Untagged image')
        self.assertNotIn('env', rows[0])
        self.assertFalse(any(row['recommended'] for row in rows))

    def discover(self, probe, controller=A):
        lines = '\n'.join([metadata(B, ['agent0ai/agent-zero:latest']), metadata(A, ['custom/controller:dev']), metadata(C, ['python:slim'])])
        with patch.object(images, 'docker', side_effect=[result(B+'\n'+A+'\n'+A+'\n'+C), result(lines)]) as cli, patch.object(images, 'current_image', return_value=controller), patch.object(images, 'runtime_ready', side_effect=probe) as check:
            data = images.discover_images()
        return data, cli, check

    def test_current_instance_preferred_only_after_runtime_check(self):
        data, cli, check = self.discover([True])
        self.assertEqual(data['recommended_id'], A)
        self.assertEqual(check.call_args_list[0].args, (A,))
        self.assertEqual(check.call_count, 1)
        self.assertEqual(cli.call_args_list[1].args[-3:], (A, B, C))
        self.assertTrue(data['images'][0]['current_instance'])

    def test_official_fallback_when_current_image_check_fails(self):
        data, _, check = self.discover([False, True])
        self.assertEqual(data['recommended_id'], B)
        self.assertEqual(check.call_count, 2)

    def test_failed_checks_never_recommend_or_probe_arbitrary_images(self):
        data, _, check = self.discover([False, False])
        self.assertIsNone(data['recommended_id'])
        self.assertEqual([call.args[0] for call in check.call_args_list], [A, B])

    def test_missing_empty_or_inaccessible_docker(self):
        for response, availability, reason in [
            (FileNotFoundError(), 'unavailable', 'docker_not_found'),
            (subprocess.TimeoutExpired('docker', 10), 'unavailable', 'docker_timeout'),
            (result(code=1), 'unavailable', 'docker_unavailable'),
            (result(), 'empty', 'no_images'),
        ]:
            with self.subTest(reason=reason), patch.object(images, 'docker', side_effect=response if isinstance(response, Exception) else None, return_value=response):
                data = images.discover_images()
                self.assertEqual((data['availability'], data['reason']), (availability, reason))
                self.assertEqual(data['images'], [])

    def test_partial_inspection_retains_images_when_one_was_removed(self):
        with patch.object(images, 'docker', side_effect=[result(A+'\n'+B), result(metadata(B, ['python:slim']), 1)]), patch.object(images, 'current_image', return_value=None), patch.object(images, 'runtime_ready') as probe:
            data = images.discover_images()
        self.assertEqual([i['id'] for i in data['images']], [B])
        probe.assert_not_called()

    def test_probe_is_bounded_and_cleans_up_after_timeout(self):
        for answer, expected in [(result('{"ready":true}'), True), (result('{"ready":false}'), False), (subprocess.TimeoutExpired('docker', 8), False)]:
            with self.subTest(answer=answer), patch.object(images, 'docker', side_effect=[answer, result()]) as cli:
                self.assertEqual(images.runtime_ready(A), expected)
            args = cli.call_args_list[0].args
            for flag in ('--pull=never', '--read-only', '--cap-drop', '--security-opt'):
                self.assertIn(flag, args)
            self.assertEqual(args[args.index('--network')+1], 'none')
            self.assertEqual(args[args.index('--user')+1], '65534:65534')
            self.assertNotIn('--volume', args)
            self.assertEqual(cli.call_args_list[-1].args, ('rm', '-f', args[args.index('--name')+1]))

    def test_api_retains_native_auth_csrf_and_rejects_get(self):
        self.assertTrue(images.Images.requires_auth())
        self.assertTrue(images.Images.requires_csrf())
        self.assertEqual(images.Images.get_methods(), ['POST'])
        handler = images.Images(None, None)
        with patch.object(images, 'discover_images') as discover:
            response = asyncio.run(handler.process({}, SimpleNamespace(method='GET')))
        self.assertEqual(response.status_code, 405)
        discover.assert_not_called()

    def test_post_runs_discovery_and_returns_data(self):
        data = {'images': [], 'availability': 'empty'}
        with patch.object(images, 'discover_images', return_value=data) as discover:
            response = asyncio.run(images.Images(None, None).process({}, SimpleNamespace(method='POST')))
        self.assertEqual(response, {'success': True, 'data': data})
        discover.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
