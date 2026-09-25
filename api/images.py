"""Local Docker image discovery with a bounded, isolated runtime recommendation."""
import asyncio
import json
from pathlib import Path
import re
import socket
import subprocess
import uuid

from helpers.api import ApiHandler, Input, Output, Request, Response

IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
IMAGE_FORMAT = ('{"id":{{json .Id}},"tags":{{json .RepoTags}},'
                '"os":{{json .Os}},"architecture":{{json .Architecture}},'
                '"size_bytes":{{json .Size}}}')
PROBE = ("import importlib.util,json,sys; "
         "modules=['flask','litellm','pydantic','langchain_core','yaml']; "
         "print(json.dumps({'ready':sys.version_info>=(3,12) and "
         "all(importlib.util.find_spec(m) is not None for m in modules)}))")


def docker(*args, timeout=10):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def current_image():
    if not Path('/.dockerenv').exists():
        return None
    result = docker('container', 'inspect', '--format', '{{.Image}}', socket.gethostname())
    value = result.stdout.strip()
    return value if result.returncode == 0 and IMAGE_ID.fullmatch(value) else None


def runtime_ready(image):
    """Check the known controller/official image; never start arbitrary list entries."""
    name = 'rrsi-image-check-' + uuid.uuid4().hex
    try:
        result = docker('run', '--rm', '--pull=never', '--name', name,
            '--label', 'a0.rrsi.image_check=true', '--network', 'none', '--read-only',
            '--user', '65534:65534', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--memory', '128m', '--cpus', '1', '--pids-limit', '32',
            '--log-driver', 'none', '--entrypoint', '/usr/bin/env', image,
            '-i', 'PATH=/usr/bin:/bin', 'HOME=/tmp', 'PYTHONDONTWRITEBYTECODE=1',
            '/opt/venv-a0/bin/python', '-I', '-c', PROBE, timeout=8)
        return result.returncode == 0 and json.loads(result.stdout).get('ready') is True
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return False
    finally:
        # A timed-out Docker client can leave its container running.
        try:
            docker('rm', '-f', name, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


def image_rows(lines, controller):
    rows = {}
    for line in lines.splitlines():
        item = json.loads(line)
        ident = item.get('id', '')
        if not isinstance(ident, str) or not IMAGE_ID.fullmatch(ident):
            continue
        tags = sorted({v for v in item.get('tags') or [] if isinstance(v, str) and v != '<none>:<none>'})
        size = item.get('size_bytes')
        rows[ident] = {
            'id': ident, 'tags': tags, 'name': tags[0] if tags else 'Untagged image',
            'size_bytes': size if type(size) is int and size >= 0 else None,
            'platform': str(item.get('os') or 'unknown') + '/' + str(item.get('architecture') or 'unknown'),
            'selectable': item.get('os') == 'linux', 'current_instance': ident == controller,
            'agent_zero': ident == controller or any(t.rsplit(':', 1)[0].split('/')[-1] == 'agent-zero' for t in tags),
            'recommended': False, 'runtime_checked': False,
        }
    return list(rows.values())


def discover_images():
    empty = {'images': [], 'recommended_id': None, 'truncated': False}
    try:
        listing = docker('image', 'ls', '--quiet', '--no-trunc')
        if listing.returncode:
            return {**empty, 'availability': 'unavailable', 'reason': 'docker_unavailable'}
        ids = list(dict.fromkeys(v for v in listing.stdout.splitlines() if IMAGE_ID.fullmatch(v)))
        if not ids:
            return {**empty, 'availability': 'empty', 'reason': 'no_images'}
        controller = current_image()
        if controller in ids:
            ids.remove(controller)
            ids.insert(0, controller)
        truncated = len(ids) > 256
        result = docker('image', 'inspect', '--format', IMAGE_FORMAT, *ids[:256])
        # Docker can return the remaining metadata when one image was removed.
        if result.returncode and not result.stdout.strip():
            return {**empty, 'availability': 'unavailable', 'reason': 'refresh_required'}
        images = image_rows(result.stdout, controller)
        official = [i for i in images if any(t.startswith('agent0ai/agent-zero:') for t in i['tags'])]
        official.sort(key=lambda i: ('agent0ai/agent-zero:latest' not in i['tags'], i['name']))
        candidates = [i for i in images if i['current_instance']] + official
        checked = set()
        recommendation = None
        for item in candidates:
            if item['id'] in checked or not item['selectable']:
                continue
            if len(checked) >= 2:
                break
            checked.add(item['id'])
            item['runtime_checked'] = True
            if runtime_ready(item['id']):
                item['recommended'] = True
                recommendation = item['id']
                break
        images.sort(key=lambda i: (not i['recommended'], not i['agent_zero'], i['name'], i['id']))
        return {'images': images, 'recommended_id': recommendation, 'truncated': truncated,
                'availability': 'ready', 'reason': None}
    except FileNotFoundError:
        return {**empty, 'availability': 'unavailable', 'reason': 'docker_not_found'}
    except subprocess.TimeoutExpired:
        return {**empty, 'availability': 'unavailable', 'reason': 'docker_timeout'}
    except (OSError, ValueError, TypeError, AttributeError):
        return {**empty, 'availability': 'unavailable', 'reason': 'docker_unavailable'}


class Images(ApiHandler):
    async def process(self, input: Input, request: Request) -> Output:
        if request.method != 'POST':
            return Response('Method not allowed', 405)
        return {'success': True, 'data': await asyncio.to_thread(discover_images)}
