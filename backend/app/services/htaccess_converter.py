"""Convert Apache ``.htaccess`` directives to nginx directives.

Pure text transform behind the "Convert .htaccess" tool next to the per-site
custom nginx rules editor. The output targets a **server-block include**
context — ServerKit's generated vhosts are single ``server { }`` blocks and
custom rules are injected inside them — so no ``server {`` wrappers are ever
emitted and only directives legal at server/location level are produced.

Everything that cannot be translated is reported in ``unsupported`` with the
original 1-based line number and a reason. Nothing is silently dropped.

Entry point::

    convert(htaccess_text) -> {
        'nginx': str,          # pretty-printed nginx directives
        'notes': [str],        # human guidance about the conversion
        'unsupported': [{'line': int, 'directive': str, 'reason': str}],
    }
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# Hard cap on input size (also enforced at the API layer).
MAX_INPUT_BYTES = 256 * 1024

_INDENT = '    '

# Directives we know about but deliberately do not translate, with tailored
# reasons. Keys are lowercase first-words.
_KNOWN_UNSUPPORTED = {
    'expiresactive': 'mod_expires has no direct include-level equivalent; use nginx `expires` inside a location block manually',
    'expiresbytype': 'mod_expires has no direct include-level equivalent; use nginx `expires` inside a location block manually',
    'expiresdefault': 'mod_expires has no direct include-level equivalent; use nginx `expires` inside a location block manually',
    'addoutputfilterbytype': 'compression is global: ServerKit writes gzip (and brotli when available) for every site in conf.d/serverkit-compression.conf',
    'setoutputfilter': 'compression is global: ServerKit writes gzip (and brotli when available) for every site in conf.d/serverkit-compression.conf',
    'browsermatch': 'per-browser rules have no nginx include-level equivalent; compression is global (conf.d/serverkit-compression.conf)',
    'php_value': 'PHP settings belong in the PHP-FPM pool config or .user.ini, not nginx',
    'php_flag': 'PHP settings belong in the PHP-FPM pool config or .user.ini, not nginx',
    'php_admin_value': 'PHP settings belong in the PHP-FPM pool config or .user.ini, not nginx',
    'php_admin_flag': 'PHP settings belong in the PHP-FPM pool config or .user.ini, not nginx',
    'setenv': 'no nginx equivalent in this context (env vars are set on the app process, not the web server)',
    'setenvif': 'no nginx equivalent in this context',
    'setenvifnocase': 'no nginx equivalent in this context',
    'addtype': 'MIME type mapping is global nginx config (mime.types), not per-site',
    'addhandler': 'handler mapping has no nginx equivalent; PHP routing is set up by the vhost template',
    'addcharset': 'per-extension charsets have no nginx include-level equivalent',
    'fileetag': 'use the nginx `etag` directive manually if needed',
    'serversignature': 'controlled globally by nginx server_tokens',
    'limitrequestbody': 'use client_max_body_size in the nginx server context (site settings) instead',
    'requestheader': 'modifying request headers requires proxy_set_header in the proxy location; not translated automatically',
    'authgroupfile': 'nginx basic auth has no group support; use a dedicated htpasswd file per group',
    'satisfy': 'nginx combines auth and IP rules with satisfy any/all; set manually if needed',
    'checkspelling': 'no nginx equivalent',
    'directoryslash': 'no nginx include-level equivalent',
    'forcetype': 'use a location block with types/default_type manually',
    'rewritemap': 'RewriteMap requires nginx map{} in http context; not available in a per-site include',
}


class _Frame:
    """A parse context: the root include, a <Files>/<FilesMatch> block, an
    <IfModule> wrapper, or an opaque unknown <Tag> block."""

    def __init__(self, kind: str, tag: str = '', location: str = '',
                 line: int = 0, raw: str = ''):
        self.kind = kind            # 'root' | 'files' | 'ifmodule' | 'opaque'
        self.tag = tag              # closing-tag name for block frames
        self.location = location    # nginx location matcher for 'files'
        self.line = line
        self.raw = raw
        self.buf: List[str] = []    # emitted nginx lines for this frame
        # Per-frame access control + auth state, flushed at frame close.
        self.allows: List[str] = []
        self.denies: List[str] = []
        self.deny_all = False
        self.allow_all = False
        self.auth_basic = False
        self.auth_name: Optional[str] = None
        self.auth_userfile: Optional[str] = None
        self.require_valid_user = False


def _split(line: str) -> List[str]:
    """Split a directive line on whitespace, honoring double quotes and
    preserving backslashes (shlex would eat regex escapes)."""
    parts: List[str] = []
    buf: List[str] = []
    in_q = False
    for ch in line:
        if ch == '"':
            in_q = not in_q
        elif ch.isspace() and not in_q:
            if buf:
                parts.append(''.join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append(''.join(buf))
    return parts


def _parse_flags(token: str) -> List[str]:
    """Parse a trailing ``[R=301,L,NC]`` flags token into a list."""
    if not (token.startswith('[') and token.endswith(']')):
        return []
    return [f.strip() for f in token[1:-1].split(',') if f.strip()]


def _normalize_pattern(pattern: str) -> str:
    """Apache htaccess-context RewriteRule patterns match the URI *without*
    its leading slash; nginx matches with it. Re-anchor accordingly."""
    if pattern.startswith('^') and not pattern.startswith('^/'):
        return '^/' + pattern[1:]
    return pattern


def _normalize_subst(subst: str) -> str:
    """Ensure relative substitutions get a leading slash for nginx."""
    if subst and not subst.startswith(('/', 'http://', 'https://', '$', '-')):
        return '/' + subst
    return subst


def _is_catch_all(pattern: str) -> bool:
    return pattern in ('.', '.*', '^(.*)$', '(.*)', '^.*$', '^(.*)', '^/?(.*)$')


def _wildcard_to_regex(name: str) -> str:
    """Turn a <Files> glob-ish name into a regex fragment."""
    escaped = re.escape(name)
    escaped = escaped.replace(r'\*', '.*').replace(r'\?', '.')
    return escaped


class _Converter:
    def __init__(self) -> None:
        self.root = _Frame('root')
        self.stack: List[_Frame] = [self.root]
        self.notes: List[str] = []
        self.unsupported: List[Dict] = []
        # Pending RewriteCond lines: (lineno, raw, teststring, condpattern)
        self.conds: List[tuple] = []

    # -- helpers ----------------------------------------------------------

    def _target(self) -> _Frame:
        """Nearest frame that owns a buffer (root or files)."""
        for frame in reversed(self.stack):
            if frame.kind in ('root', 'files'):
                return frame
        return self.root

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def flag(self, lineno: int, raw: str, reason: str) -> None:
        self.unsupported.append(
            {'line': lineno, 'directive': raw, 'reason': reason})

    def emit(self, lines: List[str], source: Optional[str] = None,
             lineno: Optional[int] = None) -> None:
        buf = self._target().buf
        if buf:
            buf.append('')
        if source is not None:
            where = f' (line {lineno})' if lineno else ''
            buf.append(f'# from: {source}{where}')
        buf.extend(lines)

    # -- rewrite handling --------------------------------------------------

    def _flush_dangling_conds(self) -> None:
        for lineno, raw, _ts, _cp in self.conds:
            self.flag(lineno, raw, 'RewriteCond without a following RewriteRule')
        self.conds = []

    def _handle_rewrite_cond(self, lineno: int, raw: str, parts: List[str]) -> None:
        if len(parts) < 3:
            self.flag(lineno, raw, 'malformed RewriteCond (expected TestString and CondPattern)')
            return
        self.conds.append((lineno, raw, parts[1], parts[2]))

    def _front_controller(self, subst: str) -> Optional[str]:
        """If pending conds are the classic !-f/!-d filename checks and the
        substitution is a front controller, return the try_files target."""
        if not self.conds:
            return None
        checks = set()
        for _ln, _raw, ts, cp in self.conds:
            if ts.upper() != '%{REQUEST_FILENAME}':
                return None
            if cp not in ('!-f', '!-d', '!-l', '!-s'):
                return None
            checks.add(cp)
        if '!-f' not in checks or '!-d' not in checks:
            return None
        target = _normalize_subst(subst)
        if target.endswith('.php'):
            return f'{target}?$args'
        if re.search(r'\.\w+$', target):
            return target
        return None

    def _https_off_cond(self) -> bool:
        for _ln, _raw, ts, cp in self.conds:
            if ts.upper() == '%{HTTPS}' and cp.lower() in ('off', '!on', '!=on'):
                return True
            if ts.upper() == '%{SERVER_PORT}' and cp in ('80', '^80$'):
                return True
        return False

    def _host_conds_only(self) -> bool:
        return bool(self.conds) and all(
            ts.upper() == '%{HTTP_HOST}' for _ln, _raw, ts, _cp in self.conds)

    def _handle_rewrite_rule(self, lineno: int, raw: str, parts: List[str]) -> None:
        if len(parts) < 3:
            self.flag(lineno, raw, 'malformed RewriteRule (expected pattern and substitution)')
            self.conds = []
            return
        pattern, subst = parts[1], parts[2]
        flags = _parse_flags(parts[3]) if len(parts) > 3 else []
        upper_flags = [f.upper() for f in flags]

        redirect_status = None
        for f in upper_flags:
            if f == 'R':
                redirect_status = 302
            elif f.startswith('R='):
                try:
                    redirect_status = int(f[2:])
                except ValueError:
                    redirect_status = 302

        # --- condition-driven patterns (detected as a unit) ---------------
        if self.conds:
            fc_target = self._front_controller(subst)
            if fc_target is not None:
                self.emit([f'try_files $uri $uri/ {fc_target};'],
                          source='front-controller block (RewriteCond !-f/!-d + RewriteRule)',
                          lineno=lineno)
                self.note('Detected the standard front-controller block '
                          '(WordPress/Laravel-style) and converted it to try_files.')
                self.conds = []
                return

            if self._https_off_cond() and redirect_status:
                buf = self._target().buf
                if buf:
                    buf.append('')
                buf.append(f'# from: {raw} (line {lineno})')
                buf.append('# NOTE: ServerKit manages HTTP->HTTPS redirects separately and')
                buf.append('# HTTPS is optional — enable it in the site SSL settings instead.')
                buf.append('# Equivalent left commented out on purpose:')
                buf.append('# if ($scheme = http) {')
                buf.append(f'#     return {redirect_status if redirect_status in (301, 302) else 301} https://$host$request_uri;')
                buf.append('# }')
                self.note('An HTTPS-forcing redirect was found; ServerKit manages '
                          'HTTP->HTTPS separately (HTTPS is optional), so the '
                          'equivalent was emitted commented out.')
                self.conds = []
                return

            if self._host_conds_only() and redirect_status:
                if len(self.conds) == 1:
                    self._emit_host_redirect(lineno, raw, pattern, subst,
                                             redirect_status)
                else:
                    for c_ln, c_raw, _ts, _cp in self.conds:
                        self.flag(c_ln, c_raw,
                                  'multiple RewriteCond %{HTTP_HOST} conditions '
                                  'cannot be combined in a single nginx if')
                    self.flag(lineno, raw,
                              'depends on multiple host conditions above')
                self.conds = []
                return

            # Untranslatable condition set: flag conds + rule, drop nothing.
            for c_ln, c_raw, ts, _cp in self.conds:
                self.flag(c_ln, c_raw,
                          f'RewriteCond on {ts} cannot be translated automatically')
            self.flag(lineno, raw,
                      'skipped because its RewriteCond lines above could not be translated')
            self.conds = []
            return

        # --- plain rules ---------------------------------------------------
        pattern = _normalize_pattern(pattern)
        if 'NC' in upper_flags:
            pattern = '(?i)' + pattern
            self.note('[NC] flags were converted to a (?i) case-insensitive '
                      'regex prefix.')
        subst = _normalize_subst(subst)

        if 'QSA' in upper_flags:
            if '?' in subst:
                self.note('[QSA] with a query string in the substitution: nginx '
                          'replaces the query string when "?" is present — append '
                          '&$args manually if the original args must be kept.')
            else:
                self.note('[QSA] is the nginx default: the original query string '
                          'is appended automatically unless the target contains "?".')

        if 'F' in upper_flags:
            self.emit([f'location ~ {pattern} {{',
                       f'{_INDENT}return 403;', '}'],
                      source=raw, lineno=lineno)
            return
        if 'G' in upper_flags:
            self.emit([f'location ~ {pattern} {{',
                       f'{_INDENT}return 410;', '}'],
                      source=raw, lineno=lineno)
            return

        if subst == '-':
            self.flag(lineno, raw,
                      'passthrough rule ("-" substitution) with no translatable '
                      'flag has no effect in nginx')
            return

        if redirect_status:
            status = redirect_status if redirect_status in (301, 302, 303, 307, 308) else 302
            self.emit([f'location ~ {pattern} {{',
                       f'{_INDENT}return {status} {subst};', '}'],
                      source=raw, lineno=lineno)
            self.note('External redirects were emitted as location + return; '
                      '$1..$9 backreferences come from the location regex.')
            return

        suffix = ' last' if 'L' in upper_flags else ''
        if 'L' in upper_flags:
            self.note('[L] flags map to the nginx rewrite "last" modifier '
                      '(stop processing and re-run location matching).')
        self.emit([f'rewrite {pattern} {subst}{suffix};'],
                  source=raw, lineno=lineno)

    def _emit_host_redirect(self, lineno: int, raw: str, pattern: str,
                            subst: str, status: int) -> None:
        c_ln, c_raw, _ts, cp = self.conds[0]
        negated = cp.startswith('!')
        if negated:
            cp = cp[1:]
        # Literal host match: ^escaped\.host$ with no other regex metachars.
        literal = None
        m = re.fullmatch(r'\^([A-Za-z0-9\\.\-]+)\$', cp)
        if m and not re.search(r'[\[\](){}|*+?]', m.group(1)):
            literal = m.group(1).replace(r'\.', '.')
        if literal is not None:
            cond = f'$host {"!=" if negated else "="} {literal}'
        else:
            cond = f'$host {"!~*" if negated else "~*"} {cp}'

        target = subst
        if _is_catch_all(pattern):
            if target.endswith('/$1'):
                target = target[:-3] + '$request_uri'
            elif target.endswith('$1'):
                target = target[:-2] + '$request_uri'
        if '%1' in target or re.search(r'%\d', target):
            self.flag(c_ln, c_raw, 'RewriteCond backreferences (%N) are not supported')
            self.flag(lineno, raw, 'uses RewriteCond backreferences (%N)')
            return

        status = status if status in (301, 302) else 301
        self.emit([f'if ({cond}) {{',
                   f'{_INDENT}return {status} {target};', '}'],
                  source=f'{c_raw} + {raw}', lineno=lineno)
        self.note('Host-based redirects were converted to an if ($host ...) '
                  'block; a dedicated server block per hostname is cleaner if '
                  'you control the full vhost.')

    # -- simple directives ---------------------------------------------------

    def _handle_redirect(self, lineno: int, raw: str, parts: List[str]) -> None:
        word = parts[0].lower()
        args = parts[1:]
        status = 302
        if word == 'redirectpermanent':
            status = 301
        elif word == 'redirecttemp':
            status = 302
        elif word == 'redirect' and args:
            token = args[0].lower()
            mapped = {'permanent': 301, 'temp': 302, 'seeother': 303,
                      'gone': 410}.get(token)
            if mapped is not None:
                status = mapped
                args = args[1:]
            elif token.isdigit():
                status = int(token)
                args = args[1:]

        if status == 410:
            if not args:
                self.flag(lineno, raw, 'Redirect gone requires a path')
                return
            path = args[0]
            self.emit([f'location {path} {{', f'{_INDENT}return 410;', '}'],
                      source=raw, lineno=lineno)
            return

        if len(args) < 2:
            self.flag(lineno, raw, 'malformed Redirect (expected path and target URL)')
            return
        path, url = args[0], args[1]
        if not path.startswith('/'):
            self.flag(lineno, raw, 'Redirect path must start with /')
            return
        if status == 301:
            self.emit([f'rewrite ^{re.escape(path)}(/.*)?$ {url}$1 permanent;'],
                      source=raw, lineno=lineno)
        elif status == 302:
            self.emit([f'rewrite ^{re.escape(path)}(/.*)?$ {url}$1 redirect;'],
                      source=raw, lineno=lineno)
        else:
            self.emit([f'location = {path} {{',
                       f'{_INDENT}return {status} {url};', '}'],
                      source=raw, lineno=lineno)
            self.note(f'Redirect with status {status}: nginx rewrite flags only '
                      'support 301/302, so an exact-match location was used '
                      '(sub-paths are not carried over).')
        self.note('Apache Redirect carries the request remainder onto the '
                  'target; the emitted rewrite preserves that with $1.')

    def _handle_redirect_match(self, lineno: int, raw: str, parts: List[str]) -> None:
        args = parts[1:]
        status = 302
        if args and (args[0].isdigit() or args[0].lower() in ('permanent', 'temp', 'gone')):
            token = args[0].lower()
            status = {'permanent': 301, 'temp': 302, 'gone': 410}.get(
                token, int(args[0]) if args[0].isdigit() else 302)
            args = args[1:]
        if status == 410:
            if not args:
                self.flag(lineno, raw, 'malformed RedirectMatch')
                return
            self.emit([f'location ~ {args[0]} {{', f'{_INDENT}return 410;', '}'],
                      source=raw, lineno=lineno)
            return
        if len(args) < 2:
            self.flag(lineno, raw, 'malformed RedirectMatch (expected regex and target URL)')
            return
        regex, url = args[0], args[1]
        status = status if status in (301, 302, 303, 307, 308) else 302
        self.emit([f'location ~ {regex} {{',
                   f'{_INDENT}return {status} {url};', '}'],
                  source=raw, lineno=lineno)

    def _handle_error_document(self, lineno: int, raw: str) -> None:
        m = re.match(r'(?i)ErrorDocument\s+(\d{3})\s+(.+)$', raw)
        if not m:
            self.flag(lineno, raw, 'malformed ErrorDocument')
            return
        code, target = m.group(1), m.group(2).strip()
        if target.startswith('"') or target.startswith("'"):
            self.flag(lineno, raw,
                      'ErrorDocument with an inline text message has no nginx '
                      'equivalent; point it at an error page file instead')
            return
        self.emit([f'error_page {code} {target};'], source=raw, lineno=lineno)
        if target.startswith(('http://', 'https://')):
            self.note('error_page with an absolute URL makes nginx answer with '
                      'a 302 redirect to it.')

    def _handle_options(self, lineno: int, raw: str, parts: List[str]) -> None:
        leftovers = []
        for token in parts[1:]:
            sign = '+'
            name = token
            if token[0] in '+-':
                sign, name = token[0], token[1:]
            lname = name.lower()
            if lname == 'indexes':
                self.emit([f'autoindex {"off" if sign == "-" else "on"};'],
                          source=raw, lineno=lineno)
            elif lname in ('followsymlinks', 'symlinksifownermatch'):
                self.note('Options FollowSymLinks has no nginx equivalent '
                          '(nginx follows symlinks by default) — omitted.')
            elif lname == 'multiviews':
                self.note('Options MultiViews (content negotiation) has no '
                          'nginx equivalent — omitted; rewrites do not need it.')
            elif lname == 'none':
                self.emit(['autoindex off;'], source=raw, lineno=lineno)
            else:
                leftovers.append(token)
        if leftovers:
            self.flag(lineno, raw,
                      f'Options {" ".join(leftovers)} has no nginx equivalent')

    def _handle_header(self, lineno: int, raw: str, parts: List[str]) -> None:
        args = parts[1:]
        if args and args[0].lower() in ('always', 'onsuccess'):
            args = args[1:]
        if not args:
            self.flag(lineno, raw, 'malformed Header directive')
            return
        action = args[0].lower()
        if action in ('unset', 'edit', 'edit*', 'echo', 'note'):
            self.flag(lineno, raw,
                      f'Header {action} has no include-level nginx equivalent')
            return
        if action not in ('set', 'add', 'append', 'merge'):
            self.flag(lineno, raw, f'unrecognized Header action "{args[0]}"')
            return
        if len(args) < 3:
            self.flag(lineno, raw, 'malformed Header directive (missing value)')
            return
        name = args[1]
        conditional = [a for a in args[3:] if a.lower().startswith(('env=', 'expr='))]
        if conditional:
            self.flag(lineno, raw,
                      'conditional Header (env=/expr=) cannot be translated')
            return
        value = ' '.join(args[2:])
        self.emit([f'add_header {name} "{value}" always;'],
                  source=raw, lineno=lineno)
        self.note('mod_headers directives became add_header ... always; note '
                  'that nginx add_header directives in a nested location '
                  'replace (not merge with) inherited ones.')

    # -- access control / auth ----------------------------------------------

    def _handle_require(self, lineno: int, raw: str, parts: List[str]) -> None:
        frame = self._target()
        args = [a.lower() for a in parts[1:]]
        if args[:2] == ['all', 'denied']:
            frame.deny_all = True
        elif args[:2] == ['all', 'granted']:
            frame.allow_all = True
        elif args[:1] == ['valid-user']:
            frame.require_valid_user = True
        elif args[:1] == ['user']:
            frame.require_valid_user = True
            self.note('Require user <name>: nginx basic auth accepts any user '
                      'in the htpasswd file — keep only the intended users in it.')
        elif args[:1] == ['ip']:
            frame.allows.extend(parts[2:])
        else:
            self.flag(lineno, raw,
                      f'Require {" ".join(parts[1:])} has no nginx equivalent')

    def _handle_allow_deny(self, lineno: int, raw: str, parts: List[str]) -> None:
        frame = self._target()
        word = parts[0].lower()
        args = parts[1:]
        if len(args) >= 2 and args[0].lower() == 'from':
            args = args[1:]
        if not args:
            self.flag(lineno, raw, f'malformed {parts[0]} directive')
            return
        if word == 'deny':
            if any(a.lower() == 'all' for a in args):
                frame.deny_all = True
            else:
                frame.denies.extend(args)
        else:  # allow
            if any(a.lower() == 'all' for a in args):
                frame.allow_all = True
            else:
                frame.allows.extend(args)

    def _flush_access_auth(self, frame: _Frame) -> None:
        lines: List[str] = []
        if frame.auth_basic or (frame.require_valid_user and frame.auth_userfile):
            name = frame.auth_name or 'Restricted'
            userfile = frame.auth_userfile or '/etc/nginx/.htpasswd'
            lines.append(f'auth_basic "{name}";')
            lines.append(f'auth_basic_user_file {userfile};')
            if not frame.auth_userfile:
                self.note('No AuthUserFile found — a placeholder htpasswd path '
                          'was used; adjust it.')
            self.note('Basic auth: upload the htpasswd file to the server and '
                      'make sure auth_basic_user_file points at its real path.')
        elif frame.require_valid_user:
            self.note('Require valid-user was found without a usable AuthType '
                      'Basic + AuthUserFile pair — basic auth was not emitted.')

        for d in frame.denies:
            lines.append(f'deny {d};')
        if frame.allow_all and not frame.allows:
            lines.append('allow all;')
        for a in frame.allows:
            lines.append(f'allow {a};')
        if frame.allows and not frame.allow_all:
            lines.append('deny all;')
            self.note('Allow/Require ip entries are emitted before a final '
                      'deny all; (nginx evaluates allow/deny in order).')
        elif frame.deny_all:
            lines.append('deny all;')

        if lines:
            if frame.buf:
                frame.buf.append('')
            frame.buf.append('# access control / auth (from Allow/Deny/Require/Auth* directives)')
            frame.buf.extend(lines)

    # -- block tags ------------------------------------------------------------

    def _open_block(self, lineno: int, raw: str) -> bool:
        m = re.match(r'<IfModule\s+(!?[\w./\-]+)\s*>$', raw, re.IGNORECASE)
        if m:
            self.stack.append(_Frame('ifmodule', tag='ifmodule',
                                     line=lineno, raw=raw))
            self.note(f'<IfModule {m.group(1)}> wrapper removed — its contents '
                      'were processed directly.')
            return True
        m = re.match(r'<Files\s+"?(.+?)"?\s*>$', raw, re.IGNORECASE)
        if m and not raw.lower().startswith('<filesmatch'):
            regex = _wildcard_to_regex(m.group(1))
            frame = _Frame('files', tag='files',
                           location=f'~ /{regex}$', line=lineno, raw=raw)
            self.stack.append(frame)
            return True
        m = re.match(r'<FilesMatch\s+"?(.+?)"?\s*>$', raw, re.IGNORECASE)
        if m:
            frame = _Frame('files', tag='filesmatch',
                           location=f'~ {m.group(1)}', line=lineno, raw=raw)
            self.stack.append(frame)
            return True
        m = re.match(r'<(\w+)[^>]*>$', raw)
        if m:
            self.stack.append(_Frame('opaque', tag=m.group(1).lower(),
                                     line=lineno, raw=raw))
            self.flag(lineno, raw,
                      f'<{m.group(1)}> blocks are not translated; their '
                      'contents were still scanned individually')
            return True
        return False

    def _close_block(self, lineno: int, raw: str) -> bool:
        m = re.match(r'</(\w+)\s*>$', raw)
        if not m:
            return False
        tag = m.group(1).lower()
        # Find the matching open frame (tolerate mismatched close tags).
        idx = None
        for i in range(len(self.stack) - 1, 0, -1):
            f = self.stack[i]
            if f.tag == tag or (tag == 'ifmodule' and f.kind == 'ifmodule') or \
               (tag in ('files', 'filesmatch') and f.kind == 'files'):
                idx = i
                break
        if idx is None:
            self.flag(lineno, raw, f'unmatched closing tag </{tag}>')
            return True
        frame = self.stack.pop(idx)
        if frame.kind == 'files':
            self._flush_access_auth(frame)
            parent = self._target()
            if frame.buf:
                if parent.buf:
                    parent.buf.append('')
                parent.buf.append(f'# from: {frame.raw} (line {frame.line})')
                parent.buf.append(f'location {frame.location} {{')
                parent.buf.extend(
                    _INDENT + l if l else '' for l in frame.buf)
                parent.buf.append('}')
                self.note('<Files>/<FilesMatch> wrappers became location '
                          'regex blocks; Apache matches file names anywhere '
                          'on disk while nginx matches the request URI — '
                          'review nested paths.')
        return True

    # -- main loop ----------------------------------------------------------

    def feed(self, lineno: int, raw: str) -> None:
        line = raw.strip()
        if not line or line.startswith('#'):
            return

        if line.startswith('</'):
            if self._close_block(lineno, line):
                return
        if line.startswith('<'):
            if self._open_block(lineno, line):
                return
            self.flag(lineno, line, 'unrecognized configuration block')
            return

        parts = _split(line)
        if not parts:
            return
        word = parts[0].lower()

        if word != 'rewritecond' and word != 'rewriterule' and self.conds:
            # Conditions only apply to the next RewriteRule.
            self._flush_dangling_conds()

        if word == 'rewriteengine':
            self.note('RewriteEngine directives were dropped (implicit in nginx).')
        elif word == 'rewritebase':
            base = parts[1] if len(parts) > 1 else '/'
            self.note(f'RewriteBase {base} was dropped — nginx rewrites always '
                      'work on the full URI; verify the emitted paths include '
                      'the base prefix if the site lives in a subdirectory.')
        elif word == 'rewriteoptions':
            self.flag(lineno, line, 'RewriteOptions has no nginx equivalent')
        elif word == 'rewritecond':
            self._handle_rewrite_cond(lineno, line, parts)
        elif word == 'rewriterule':
            self._handle_rewrite_rule(lineno, line, parts)
        elif word in ('redirect', 'redirectpermanent', 'redirecttemp'):
            self._handle_redirect(lineno, line, parts)
        elif word == 'redirectmatch':
            self._handle_redirect_match(lineno, line, parts)
        elif word == 'errordocument':
            self._handle_error_document(lineno, line)
        elif word == 'options':
            self._handle_options(lineno, line, parts)
        elif word == 'directoryindex':
            if len(parts) > 1:
                self.emit([f'index {" ".join(parts[1:])};'],
                          source=line, lineno=lineno)
            else:
                self.flag(lineno, line, 'malformed DirectoryIndex')
        elif word == 'adddefaultcharset':
            if len(parts) > 1 and parts[1].lower() != 'off':
                self.emit([f'charset {parts[1]};'], source=line, lineno=lineno)
        elif word == 'header':
            self._handle_header(lineno, line, parts)
        elif word == 'authtype':
            if len(parts) > 1 and parts[1].lower() == 'basic':
                self._target().auth_basic = True
            else:
                self.flag(lineno, line,
                          f'AuthType {parts[1] if len(parts) > 1 else "?"} is '
                          'not supported (only Basic maps to nginx auth_basic)')
        elif word == 'authname':
            self._target().auth_name = ' '.join(parts[1:]) if len(parts) > 1 else None
        elif word == 'authuserfile':
            self._target().auth_userfile = parts[1] if len(parts) > 1 else None
        elif word == 'require':
            if len(parts) < 2:
                self.flag(lineno, line, 'malformed Require directive')
            else:
                self._handle_require(lineno, line, parts)
        elif word in ('allow', 'deny'):
            self._handle_allow_deny(lineno, line, parts)
        elif word == 'order':
            self.note('Order directives were dropped; the emitted allow/deny '
                      'lines already encode the effective policy in nginx order.')
        elif word in _KNOWN_UNSUPPORTED:
            self.flag(lineno, line, _KNOWN_UNSUPPORTED[word])
        else:
            self.flag(lineno, line,
                      'unrecognized directive — no nginx equivalent known')

    def finish(self) -> Dict:
        self._flush_dangling_conds()
        # Close any unclosed <Files>/<FilesMatch> blocks gracefully.
        while len(self.stack) > 1:
            frame = self.stack[-1]
            if frame.kind == 'files':
                self._close_block(frame.line, f'</{frame.tag}>')
                self.flag(frame.line, frame.raw,
                          'block was never closed — treated as closed at EOF')
            else:
                self.stack.pop()
        self._flush_access_auth(self.root)

        nginx = '\n'.join(self.root.buf).strip('\n')
        if nginx:
            nginx += '\n'
        return {
            'nginx': nginx,
            'notes': self.notes,
            'unsupported': self.unsupported,
        }


def convert(htaccess_text: str) -> Dict:
    """Convert ``.htaccess`` text to nginx directives.

    Returns ``{'nginx': str, 'notes': [str], 'unsupported': [dict]}``.
    Raises ``ValueError`` if the input exceeds :data:`MAX_INPUT_BYTES`.
    """
    if htaccess_text is None:
        htaccess_text = ''
    if not isinstance(htaccess_text, str):
        raise ValueError('htaccess_text must be a string')
    if len(htaccess_text.encode('utf-8', errors='replace')) > MAX_INPUT_BYTES:
        raise ValueError('.htaccess input exceeds the 256KB limit')

    conv = _Converter()
    # Handle Apache line continuations (trailing backslash).
    logical: List[tuple] = []
    pending = ''
    pending_line = 0
    for i, raw in enumerate(htaccess_text.splitlines(), start=1):
        if pending:
            merged = pending + ' ' + raw.strip()
        else:
            merged = raw
            pending_line = i
        if merged.rstrip().endswith('\\'):
            pending = merged.rstrip()[:-1].rstrip()
            continue
        logical.append((pending_line, merged))
        pending = ''
    if pending:
        logical.append((pending_line, pending))

    for lineno, line in logical:
        conv.feed(lineno, line)
    return conv.finish()
