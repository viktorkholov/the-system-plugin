#!/usr/bin/env python3
"""Get the `sys` plugin from the-system, for a person who signed in.

Claude Code runs this when the plugin is installed and once per session after
that (the `command` source in `.claude-plugin/marketplace.json`). It prints one
line: the directory that holds the plugin. Claude Code copies that directory
into its plugin cache.

This file is public. It holds no address, no token and no server code. What
it does:

1. Finds the token Claude Code keeps for the-system after
   `/mcp` -> the-system -> Authenticate. That is the Keychain item
   `Claude Code-credentials` on macOS (with a suffix for a custom
   `CLAUDE_CONFIG_DIR`), or `.credentials.json` in the Claude Code
   configuration directory. The plugin's own sign-in (`/sys:sign-in`)
   is the second choice.
2. Asks the server that token was made for, and no other server, for
   `<its MCP address>/plugin/sys.zip`. The server answers 401 to anyone who
   is not signed in.
3. Unpacks the zip into `<config dir>/the-system/plugin-<hash>` and prints
   that path.

It never refreshes a token: the refresh token belongs to Claude Code, and the
server replaces it each time it is used. If the token has run out, open
Claude Code, run /mcp once so it renews the token, and run the install again.

Standard library only, because this runs on other people's machines.
"""

import hashlib
import io
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

PLUGIN_NAME = 'sys'
SERVER_NAME = 'the-system'
#: How Claude Code names our server in its store: the plugin's server first,
#: then an address connection (`claude mcp add ... the-system <address>`).
SERVER_NAMES = (f'plugin:{PLUGIN_NAME}:{SERVER_NAME}', f'plugin:infuse:{SERVER_NAME}', SERVER_NAME)
STORE_SERVICE = 'Claude Code-credentials'
STORE_FILE = '.credentials.json'
#: Added to the MCP address the token was made for. The server holds the
#: same string (`backend/thesystem/mcp/plugin_bundle.py`), and a test holds
#: the two together.
BUNDLE_SUFFIX = f'/plugin/{PLUGIN_NAME}.zip'
LOOPBACK = ('localhost', '127.0.0.1', '::1')
MARGIN_SECONDS = 30
TIMEOUT_SECONDS = 60
KEYCHAIN_TIMEOUT_SECONDS = 3
#: The plugin is a few hundred kilobytes. Anything this large is not it.
MAX_BYTES = 20 * 1024 * 1024

NOT_SIGNED_IN = (
    'Could not get the sys plugin: you are not signed in to the-system yet.\n'
    'First connect the MCP address and sign in:\n'
    '  claude mcp add --transport http --scope user the-system <address>\n'
    '  then in Claude Code: /mcp -> the-system -> Authenticate\n'
    'The address is on the "Set up platform as MCP" page of your dashboard.\n'
    'Then run the install again: claude plugin install sys@the-system'
)
REFUSED = (
    'Could not get the sys plugin: {server} did not accept your sign-in ({status}).\n'
    'In Claude Code, run /mcp -> the-system -> Authenticate again,\n'
    'then run: claude plugin install sys@the-system'
)
FAILED = 'Could not get the sys plugin from {server}: {reason}'


def config_dir() -> Path:
    configured = os.environ.get('CLAUDE_CONFIG_DIR')
    return Path(configured) if configured else Path.home() / '.claude'


def _read_json(path: Path) -> dict:
    try:
        parsed = json.loads(path.read_text())
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def store_service() -> str:
    """The Keychain item for the active configuration directory.

    With `CLAUDE_CONFIG_DIR` set, Claude Code names it `Claude Code-credentials-`
    plus the first eight hex digits of the SHA-256 of that directory as the
    variable spells it (measured on 2.1.280, 2026-09-23). Claude Code runs this
    script with the same variable, so the name is this profile's own. The same
    rule as `keychain_service` in the plugin's `statusline/credentials.py`.
    """
    configured = os.environ.get('CLAUDE_CONFIG_DIR')
    if not configured:
        return STORE_SERVICE
    return '{}-{}'.format(STORE_SERVICE, hashlib.sha256(configured.encode()).hexdigest()[:8])


def claude_store() -> dict:
    """Claude Code's credential store: the file where there is one, else the Keychain."""
    path = config_dir() / STORE_FILE
    found = _read_json(path) if path.exists() else {}
    # A file with no MCP sign-ins in it (left by an older Claude Code, or by a
    # Keychain that was locked once) does not hide the Keychain's.
    if found.get('mcpOAuth') or sys.platform != 'darwin':
        return found
    try:
        answer = subprocess.run(
            ['security', 'find-generic-password', '-s', store_service(), '-w'],
            capture_output=True, text=True, timeout=KEYCHAIN_TIMEOUT_SECONDS, check=False,
        )
        parsed = json.loads(answer.stdout.strip()) if answer.returncode == 0 else {}
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def dialable(url: str) -> str:
    """The address without a trailing slash, if a token may go there; else ''.

    `https`, or plain `http` only on this machine (a local stack).
    """
    url = str(url or '').strip().rstrip('/')
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ''
    if parts.scheme == 'https' and parts.hostname:
        return url
    if parts.scheme == 'http' and parts.hostname in LOOPBACK:
        return url
    return ''


def candidates(now: float = None) -> list:
    """`(address, token)` pairs to try, best first, each token next to the address it was made for."""
    now = time.time() if now is None else now
    ranked = []
    for key, record in ((claude_store().get('mcpOAuth') or {}).items()):
        if not isinstance(record, dict):
            continue
        name = str(record.get('serverName') or str(key).split('|')[0])
        if name not in SERVER_NAMES:
            continue
        token = str(record.get('accessToken') or '').strip()
        url = dialable(record.get('serverUrl'))
        expires_at = record.get('expiresAt')
        if not token or not url:
            continue
        if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
            expires_at = 0
        elif expires_at / 1000 <= now + MARGIN_SECONDS:
            continue
        ranked.append(((SERVER_NAMES.index(name), -expires_at), url, token))
    found = [(url, token) for _, url, token in sorted(ranked, key=lambda row: row[0])]
    own = _read_json(config_dir() / SERVER_NAME / 'token.json')
    token = str(own.get('access_token') or '').strip()
    url = dialable(own.get('url'))
    expires_at = own.get('expires_at')
    fresh = not isinstance(expires_at, (int, float)) or expires_at > now + MARGIN_SECONDS
    if token and url and fresh and (url, token) not in found:
        found.append((url, token))
    return found


def _download_with_curl(url: str, token: str) -> tuple:
    """For a python3 with no root certificates (python.org's macOS build).

    The header goes to curl on stdin, not on its command line, so other
    users on the machine cannot read the token from the process list.
    """
    with tempfile.NamedTemporaryFile(delete=False) as out:
        target = out.name
    try:
        answer = subprocess.run(
            ['curl', '-sS', '--max-time', str(TIMEOUT_SECONDS), '--max-filesize', str(MAX_BYTES),
             '-K', '-', '-o', target, '-w', '%{http_code}', url],
            input=f'header = "Authorization: Bearer {token}"\n',
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS + 5, check=False,
        )
        status = int(answer.stdout.strip() or 0) if answer.stdout.strip().isdigit() else 0
        if status == 0:
            raise OSError((answer.stderr or 'curl failed').strip())
        return status, Path(target).read_bytes()
    finally:
        os.unlink(target)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would carry the `Authorization` header to
    wherever `Location` points, and the token is for this address only."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def download(url: str, token: str) -> tuple:
    """`(status, body)`. Raises OSError when the server could not be reached at all."""
    request = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {token}', 'Accept': 'application/zip',
        'User-Agent': 'the-system-plugin-installer',
    })
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as answer:
            return answer.status, answer.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as answer:
        return answer.code, answer.read(64 * 1024)
    except urllib.error.URLError as failure:
        if isinstance(failure.reason, ssl.SSLCertVerificationError) and shutil.which('curl'):
            return _download_with_curl(url, token)
        raise OSError(str(failure.reason)) from failure


#: How long an older copy is kept after a newer one arrives. Claude Code copies
#: the printed folder right after the command exits; a session that started a
#: moment earlier may still be copying the one it was given.
KEEP_OLD_SECONDS = 10 * 60


def _valid(folder: Path) -> bool:
    try:
        return json.loads((folder / '.claude-plugin' / 'plugin.json').read_text()).get('name') == PLUGIN_NAME
    except Exception:
        return False


def _prune(parent: Path, keep: Path, now: float) -> None:
    for other in parent.glob('plugin-*'):
        if other == keep or not other.is_dir():
            continue
        try:
            if now - other.stat().st_mtime > KEEP_OLD_SECONDS:
                shutil.rmtree(other, ignore_errors=True)
        except OSError:
            pass


def unpack(body: bytes, parent: Path) -> Path:
    """The zip into `parent/plugin-<hash of the zip>`, whole or not at all.

    Named by content, so two sessions that start together and fetch the same
    plugin agree on one folder instead of swapping each other's out from under
    Claude Code's copy. The folder is renamed into place in one step; the
    loser of a race drops its own copy and uses the winner's. Refuses anything
    that is not our plugin.
    """
    if len(body) > MAX_BYTES:
        raise ValueError('the answer is too large to be the plugin')
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as failure:
        raise ValueError('the answer is not a zip file') from failure
    target = parent / f'plugin-{hashlib.sha256(body).hexdigest()[:16]}'
    with archive:
        for name in archive.namelist():
            path = Path(name)
            if path.is_absolute() or '..' in path.parts or name.startswith(('/', '\\')):
                raise ValueError(f'the zip has an unsafe path: {name}')
        try:
            manifest = json.loads(archive.read('.claude-plugin/plugin.json'))
        except Exception as failure:
            raise ValueError('the zip has no .claude-plugin/plugin.json') from failure
        if manifest.get('name') != PLUGIN_NAME:
            raise ValueError(f'the zip holds {manifest.get("name")!r}, not {PLUGIN_NAME!r}')
        parent.mkdir(parents=True, exist_ok=True)
        if not _valid(target):
            fresh = Path(tempfile.mkdtemp(prefix='.new-', dir=str(parent)))
            try:
                for info in archive.infolist():
                    archive.extract(info, fresh)
                    mode = (info.external_attr >> 16) & 0o777
                    if mode and not info.is_dir():
                        os.chmod(fresh / info.filename, mode)
                try:
                    os.rename(fresh, target)
                except OSError:
                    # Another session put the same plugin there first.
                    if not _valid(target):
                        raise
            finally:
                shutil.rmtree(fresh, ignore_errors=True)
    now = time.time()
    os.utime(target, (now, now))
    _prune(parent, target, now)
    return target


def main() -> int:
    found = candidates()
    if not found:
        print(NOT_SIGNED_IN, file=sys.stderr)
        return 1
    last = ''
    for server, token in found:
        try:
            status, body = download(server + BUNDLE_SUFFIX, token)
        except OSError as failure:
            last = FAILED.format(server=server, reason=failure)
            continue
        if status == 401:
            last = REFUSED.format(server=server, status=status)
            continue
        if status == 403:
            # Signed in, and the account is refused (deactivated, admins only):
            # the server's sentence says why; signing in again would not help.
            reason = body[:500].decode('utf-8', 'replace').strip() or 'HTTP 403'
            last = FAILED.format(server=server, reason=reason)
            continue
        if status != 200:
            reason = body[:300].decode('utf-8', 'replace').strip() or f'HTTP {status}'
            last = FAILED.format(server=server, reason=reason)
            continue
        try:
            path = unpack(body, config_dir() / SERVER_NAME)
        except (ValueError, OSError) as failure:
            last = FAILED.format(server=server, reason=failure)
            continue
        print(path)
        return 0
    print(last, file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
