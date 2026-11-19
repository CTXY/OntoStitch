"""A local cross-process limit for complete synchronous HTTP exchanges."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit


@contextmanager
def request_lease(directory, limit, *, wait_timeout=3600):
    if limit < 1:
        raise ValueError('request limit must be positive')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_timeout
    start = (os.getpid() + threading.get_ident()) % limit
    while True:
        for offset in range(limit):
            handle = (directory / f'{(start + offset) % limit}.lock').open('a')
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()
            return
        if time.monotonic() >= deadline:
            raise TimeoutError('Timed out waiting for a shared model request slot')
        time.sleep(0.1)


def _credential_route(url, credential):
    parsed = urlsplit(str(url))
    origin = f'{parsed.scheme.lower()}://{parsed.netloc.lower()}'
    return hashlib.sha256((origin + '\0' + credential).encode()).hexdigest()


def limit_client_requests(client, directory, limit, *, credential=None, base_url=None):
    """Hold a lease through response consumption, including SDK streaming sends.

    The batch audit already buffers SSE before handing it to the SDK. Reading
    here preserves that behavior and also closes the lifecycle for other callers.
    """
    scope = (str(Path(directory).resolve()), limit)
    installed = getattr(client, '_cortex_request_limit', None)
    if credential is not None:
        # LangChain caches HTTP clients without including the API key in its
        # cache key. Route each request by its actual Authorization header so a
        # key rotation neither double-wraps the client nor changes an old lease.
        routes = getattr(client, '_cortex_credential_request_limits', None)
        if routes is None:
            routes = {}
            client._cortex_credential_request_limits = routes
        route = _credential_route(base_url, credential)
        previous = routes.get(route)
        if previous is not None and previous != scope:
            raise ValueError('Credential already has a different shared request limit')
        routes[route] = scope
    if installed is not None:
        if credential is None and installed != scope:
            raise ValueError('HTTP client already has a different shared request limit')
        return
    original = client.send

    def send(request, *args, **kwargs):
        selected = scope
        authorization = request.headers.get('authorization', '')
        scheme, _, token = authorization.partition(' ')
        if scheme.lower() == 'bearer':
            route = _credential_route(request.url, token)
            selected = getattr(client, '_cortex_credential_request_limits', {}).get(route, scope)
        with request_lease(selected[0], selected[1]):
            response = original(request, *args, **kwargs)
            try:
                response.read()
                return response
            except BaseException:
                response.close()
                raise

    client.send = send
    client._cortex_request_limit = scope
