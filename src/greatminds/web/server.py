"""Small same-origin HTTP transport. No login, cookies, tokens or CORS API."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import urlsplit, parse_qs

import yaml
from greatminds.core.errors import GreatMindsError
from greatminds.runtime.interactions import ConversationStore


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service):
        self.service = service
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = 'Greatminds'

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *_args):
        pass  # Do not put prompts or request query strings into console logs.

    def reply(self, value, status=200, mime='application/json; charset=utf-8'):
        content = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                      **{f'/{name}.js': (f'{name}.js', 'text/javascript; charset=utf-8') for name in ('marked', 'purify', 'markdown', 'i18n', 'stands')},
                      '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if path in assets:
                name, mime = assets[path]
                content = files('greatminds.web').joinpath('assets', name).read_bytes()
                if name == 'index.html':
                    content = content.replace(b'<html lang="en">', ('<html lang="en" data-default-language="' + self.server.service.default_language() + '">').encode())
                    content = content.replace(b'id="app-version">v\xe2\x80\x94', ('id="app-version">v' + self.server.service.version).encode())
                self.reply(content, mime=mime)
                return
            service = self.server.service
            if path == '/api/state':
                self.reply(service.overview())
            elif path == '/api/stands':
                from .stands import Stands
                self.reply(Stands(service).snapshot())
            elif path == '/api/settings':
                self.reply(service.settings())
            else:
                parts = path.strip('/').split('/')
                after = int(parse_qs(parsed.query).get('after', ['0'])[0])
                if after < 0:
                    raise ValueError('Invalid cursor')
                if len(parts) == 3 and parts[:2] == ['api', 'conversations']:
                    self.reply(service.conversation(parts[2]))
                elif len(parts) == 4 and parts[:2] == ['api', 'conversations'] and parts[3] == 'events':
                    self.reply(ConversationStore(service.runtime, parts[2]).events(after=after))
                elif len(parts) == 3 and parts[:2] == ['api', 'runs']:
                    self.reply(service.run_detail(parts[2], after))
                elif len(parts) == 3 and parts[:2] == ['api', 'tasks']:
                    self.reply(service.task_detail(parts[2]))
                else:
                    self.reply({'error': 'Not found'}, 404)
        except (GreatMindsError, ValueError, KeyError, TypeError, OSError, yaml.YAMLError) as exc:
            self.error(exc)

    def do_POST(self):
        try:
            origin = self.headers.get('Origin')
            if origin and (urlsplit(origin).scheme not in {'http', 'https'} or
                           urlsplit(origin).netloc != self.headers.get('Host')):
                self.reply({'error': 'Cross-origin writes are not allowed'}, 403)
                return
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                self.reply({'error': 'Use application/json'}, 415)
                return
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1048576:
                self.reply({'error': 'Request body must be 1–1048576 bytes'}, 413)
                return
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('Expected a JSON object')
            self.reply(self.server.service.action(urlsplit(self.path).path, body))
        except (GreatMindsError, ValueError, KeyError, TypeError, OSError, yaml.YAMLError) as exc:
            self.error(exc)

    def error(self, exc):
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        if isinstance(exc, GreatMindsError):
            self.reply({'error': str(exc)}, 409 if exc.exit_code in {3, 4} else 400)
        elif isinstance(exc, OSError):
            self.reply({'error': 'Cannot read or write project state. Check paths and permissions.'}, 400)
        else:
            self.reply({'error': 'Invalid request or settings. Check required fields and values.'}, 400)
