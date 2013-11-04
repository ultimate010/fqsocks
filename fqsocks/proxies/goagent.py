# thanks @phuslu modified from https://github.com/goagent/goagent/blob/2.0/local/proxy.py
# coding:utf-8
import logging
import socket
import time
import sys
import re
import functools
import fnmatch
import urllib
import httplib
import random
import contextlib
import zlib
import struct
import io
import copy
import threading

import ssl
import gevent.queue

from .. import networking
from .. import stat
from .direct import Proxy
from .http_try import recv_and_parse_request, NotHttp
from .http_try import CapturingSock


try:
    import urllib.request
    import urllib.parse
except ImportError:
    import urllib

    urllib.request = __import__('urllib2')
    urllib.parse = __import__('urlparse')

try:
    import queue
except ImportError:
    import Queue as queue

try:
    import http.server
    import http.client
except ImportError:
    http = type(sys)('http')
    http.server = __import__('BaseHTTPServer')
    http.client = __import__('httplib')
    http.client.parse_headers = http.client.HTTPMessage

LOGGER = logging.getLogger(__name__)

RE_VERSION = re.compile(r'\d+\.\d+\.\d+')
SKIP_HEADERS = frozenset(
    ['Vary', 'Via', 'X-Forwarded-For', 'Proxy-Authorization', 'Proxy-Connection', 'Upgrade', 'X-Chrome-Variations',
     'Connection', 'Cache-Control'])
ABBV_HEADERS = {'Accept': ('A', lambda x: '*/*' in x),
                'Accept-Charset': ('AC', lambda x: x.startswith('UTF-8,')),
                'Accept-Language': ('AL', lambda x: x.startswith('zh-CN')),
                'Accept-Encoding': ('AE', lambda x: x.startswith('gzip,')), }
GAE_OBFUSCATE = 0
GAE_PASSWORD = ''
GAE_PATH = '/2'

AUTORANGE_HOSTS = (
    '*.c.youtube.com',
    '*.atm.youku.com',
    '*.googlevideo.com',
    'av.vimeo.com',
    'smile-*.nicovideo.jp',
    'video.*.fbcdn.net',
    's*.last.fm',
    'x*.last.fm',
    '*.x.xvideos.com',
    'cdn*.pornhub.com',
    '*.edgecastcdn.net',
    '*.d.rncdn3.com',
    'cdn*.public.tube8.com',
    'videos.flv*.redtubefiles.com',
    '*.mms.vlog.xuite.net',
    'vs*.thisav.com',
    'archive.rthk.hk',
    'video*.modimovie.com')
AUTORANGE_HOSTS_MATCH = [re.compile(fnmatch.translate(h)).match for h in AUTORANGE_HOSTS]
AUTORANGE_ENDSWITH = '.f4v|.flv|.hlv|.m4v|.mp4|.mp3|.ogg|.avi|.exe|.zip|.iso|.rar|.bz2|.xz|.dmg'.split('|')
AUTORANGE_ENDSWITH = tuple(AUTORANGE_ENDSWITH)
AUTORANGE_NOENDSWITH = '.xml|.json|.html|.php|.py.js|.css|.jpg|.jpeg|.png|.gif|.ico'.split('|')
AUTORANGE_NOENDSWITH = tuple(AUTORANGE_NOENDSWITH)
AUTORANGE_MAXSIZE = 1048576
AUTORANGE_WAITSIZE = 524288
AUTORANGE_BUFSIZE = 8192
AUTORANGE_THREADS = 4
SKIP_HEADERS = frozenset(['Vary', 'Via', 'X-Forwarded-For', 'Proxy-Authorization', 'Proxy-Connection',
                          'Upgrade', 'X-Chrome-Variations', 'Connection', 'Cache-Control'])

normcookie = functools.partial(re.compile(', ([^ =]+(?:=|$))').sub, '\\r\\nSet-Cookie: \\1')


class GoAgentProxy(Proxy):
    last_refresh_started_at = 0
    gray_list = set()
    black_list = set()
    google_ip_failed_times = {}
    google_ip_latency_records = {}

    GOOGLE_HOSTS = ['www.g.cn', 'www.google.cn', 'www.google.com', 'mail.google.com']
    GOOGLE_IPS = []
    proxies = []

    def __init__(self, appid, path='/2', password=''):
        super(GoAgentProxy, self).__init__()
        assert appid
        self.appid = appid
        self.path = path or '/2'
        self.password = password
        self.version = 'UNKNOWN'
        self.flags.add('PUBLIC')

    @property
    def fetch_server(self):
        return 'https://%s.appspot.com%s?' % (self.appid, self.path)

    def query_version(self):
        try:
            ssl_sock = create_ssl_connection()
            with contextlib.closing(ssl_sock):
                with contextlib.closing(ssl_sock.sock):
                    ssl_sock.settimeout(5)
                    ssl_sock.sendall('GET https://%s.appspot.com/2 HTTP/1.1\r\n\r\n\r\n' % self.appid)
                    response = ssl_sock.recv(8192)
                    match = RE_VERSION.search(response)
                    if 'Over Quota' in response:
                        self.died = True
                        LOGGER.info('%s over quota' % self)
                        return
                    if match:
                        self.version = match.group(0)
                        LOGGER.info('queried appid version: %s' % self)
                    else:
                        LOGGER.info('failed to query appid version: %s' % response)
        except:
            LOGGER.exception('failed to query goagent %s version' % self.appid)

    def do_forward(self, client):
        try:
            if not recv_and_parse_request(client):
                raise Exception('payload is too large')
            if client.method.upper() not in ('GET', 'POST'):
                raise Exception('unsupported method: %s' % client.method)
            if client.host in GoAgentProxy.black_list:
                raise Exception('%s failed to proxy via goagent before' % client.host)
        except NotHttp:
            raise
        except:
            for proxy in self.proxies:
                client.tried_proxies[proxy] = 'skip goagent'
            LOGGER.error('[%s] failed to recv and parse request: %s' % (repr(client), sys.exc_info()[1]))
            client.fall_back(reason='failed to recv and parse request, %s' % sys.exc_info()[1])
        forward(client, self)

    @classmethod
    def is_protocol_supported(cls, protocol):
        return 'HTTP' == protocol

    @classmethod
    def refresh(cls, proxies):
        cls.proxies = proxies
        resolved_google_ips = cls.resolve_google_ips()
        if resolved_google_ips:
            for proxy in proxies:
                proxy.died = False
                gevent.spawn(proxy.query_version)
        else:
            for proxy in proxies:
                proxy.died = not resolved_google_ips
        return resolved_google_ips

    @classmethod
    def resolve_google_ips(cls):
        if cls.GOOGLE_IPS:
            return True
        LOGGER.info('resolving google ips from %s' % cls.GOOGLE_HOSTS)
        all_ips = set()
        for host in cls.GOOGLE_HOSTS:
            if re.match(r'\d+\.\d+\.\d+\.\d+', host):
                all_ips.add(host)
            else:
                ips = networking.resolve_ips(host)
                if len(ips) > 1:
                    all_ips |= set(ips)
        if not all_ips:
            LOGGER.fatal('failed to resolve google ip')
            return False
        cls.GOOGLE_IPS = list(all_ips)
        random.shuffle(cls.GOOGLE_IPS)
        return True

    def __repr__(self):
        return 'GoAgentProxy[%s ver %s]' % (self.appid, self.version)

    @property
    def public_name(self):
        return 'GoAgent\t%s' % self.appid


def forward(client, proxy):
    parsed_url = urllib.parse.urlparse(client.url)
    range_in_query = 'range=' in parsed_url.query or 'redirect_counter=' in parsed_url.query
    special_range = (any(x(client.host) for x in AUTORANGE_HOSTS_MATCH) or client.url.endswith(
        AUTORANGE_ENDSWITH)) and not client.url.endswith(
        AUTORANGE_NOENDSWITH) and not 'redirector.c.youtube.com' == client.host
    if client.host in GoAgentProxy.gray_list:
        special_range = True
    range_end = 0
    auto_ranged = False
    if 'Range' in client.headers:
        LOGGER.info('[%s] range present: %s' % (repr(client), client.headers['Range']))
        m = re.search('bytes=(\d+)-(\d*)', client.headers['Range'])
        if m:
            range_start = int(m.group(1))
            range_end = int(m.group(2)) if m.group(2) else 0
            if not range_end or range_end - range_start > AUTORANGE_MAXSIZE:
                client.headers['Range'] = 'bytes=%d-%d' % (range_start, range_start + AUTORANGE_MAXSIZE)
                LOGGER.info('[%s] adjusted range: %s' % (repr(client), client.headers['Range']))
    elif not range_in_query and special_range:
        client.headers['Range'] = 'bytes=%d-%d' % (0, AUTORANGE_MAXSIZE)
        auto_ranged = True
        LOGGER.info('[%s] auto range: %s' % (repr(client), client.headers['Range']))
    response = None
    try:
        kwargs = {}
        if proxy.password:
            kwargs['password'] = proxy.password
        if 'youtube.com/watch' in client.url or '.c.android.clients.google.com' in client.url:
            for proxy in GoAgentProxy.proxies:
                client.tried_proxies[proxy] = 'skip goagent'
            client.fall_back(reason='goagent can not proxy youtube watch')
        try:
            response = gae_urlfetch(
                client, proxy, client.method, client.url, client.headers, client.payload, **kwargs)
        except ConnectionFailed:
            for proxy in GoAgentProxy.proxies:
                client.tried_proxies[proxy] = 'skip goagent'
            client.fall_back('can not connect to google ip')
        except ReadResponseFailed:
            if 'youtube.com' not in client.host and 'googlevideo.com' not in client.host:
                GoAgentProxy.gray_list.add(client.host)
                if auto_ranged:
                    LOGGER.error('[%s] !!! blacklist goagent for %s !!!' % (repr(client), client.host))
                    GoAgentProxy.black_list.add(client.host)
            for proxy in GoAgentProxy.proxies:
                client.tried_proxies[proxy] = 'skip goagent'
            client.fall_back(reason='failed to read response from gae_urlfetch')
        if response is None:
            client.fall_back('urlfetch empty response')
        if response.app_status == 503:
            proxy.died = True
            if time.time() - GoAgentProxy.last_refresh_started_at > 60:
                GoAgentProxy.last_refresh_started_at = time.time()
                LOGGER.error('refresh goagent proxies due to over quota')
                gevent.spawn(GoAgentProxy.refresh, GoAgentProxy.proxies)
            client.fall_back('goagent server over quota')
        if response.app_status == 500:
            proxy.died = True
            client.fall_back('goagent server busy')
        if response.app_status == 404:
            proxy.died = True
            client.fall_back('goagent server not found')
        if response.app_status == 302:
            proxy.died = True
            client.fall_back('goagent server 302 moved')
        if response.app_status == 403 and 'youtube.com' in client.url:
            proxy.died = True
            client.fall_back('goagent server %s banned youtube' % proxy)
        if response.app_status != 200:
            if LOGGER.isEnabledFor(logging.DEBUG):
                LOGGER.debug('HTTP/1.1 %s\r\n%s\r\n' % (response.status, ''.join(
                    '%s: %s\r\n' % (k.title(), v) for k, v in response.getheaders() if k != 'transfer-encoding')))
                LOGGER.debug(response.read())
            client.fall_back('urlfetch failed: %s' % response.app_status)
        client.forward_started = True
        if response.status == 206:
            LOGGER.info('[%s] start range fetch' % repr(client))
            rangefetch = RangeFetch(client, range_end, auto_ranged, response)
            return rangefetch.fetch()
        if 'Set-Cookie' in response.msg:
            response.msg['Set-Cookie'] = normcookie(response.msg['Set-Cookie'])
        client.downstream_wfile.write('HTTP/1.1 %s\r\n%s\r\n' % (response.status, ''.join(
            '%s: %s\r\n' % (k.title(), v) for k, v in response.getheaders() if k != 'transfer-encoding')))
        content_length = int(response.getheader('Content-Length', 0))
        content_range = response.getheader('Content-Range', '')
        if content_range:
            start, end, length = list(map(int, re.search(r'bytes (\d+)-(\d+)/(\d+)', content_range).group(1, 2, 3)))
        else:
            start, end, length = 0, content_length - 1, content_length
        while 1:
            try:
                data = response.read(8192)
                response.ssl_sock.counter.received(len(response.counted_sock.rfile.captured))
                response.counted_sock.rfile.captured = ''
            except httplib.IncompleteRead as e:
                LOGGER.error('incomplete read: %s' % e.partial)
                raise
            if not data:
                response.close()
                return
            start += len(data)
            client.downstream_wfile.write(data)
            if start >= end:
                response.close()
                return
    finally:
        if response:
            response.close()


def _create_ssl_connection(ip, port):
    sock = None
    ssl_sock = None
    try:
        sock = networking.create_tcp_socket(ip, port, 2)
        ssl_sock = ssl.wrap_socket(sock, do_handshake_on_connect=False)
        ssl_sock.settimeout(2)
        ssl_sock.do_handshake()
        ssl_sock.sock = sock
        return ssl_sock
    except:
        if ssl_sock:
            ssl_sock.close()
        if sock:
            sock.close()
        return None


def create_ssl_connection():
    for i in range(3):
        google_ip = pick_best_google_ip()
        started_at = time.time()
        ssl_sock = _create_ssl_connection(google_ip, 443)
        if ssl_sock:
            record_google_ip_latency(google_ip, time.time() - started_at)
            ssl_sock.google_ip = google_ip
            return ssl_sock
        else:
            LOGGER.error('!!! failed to connect google ip %s !!!' % google_ip)
            GoAgentProxy.google_ip_failed_times[google_ip] = GoAgentProxy.google_ip_failed_times.get(google_ip, 0) + 1
            gevent.sleep(0.1)
    raise ConnectionFailed()


def pick_best_google_ip():
    random.shuffle(GoAgentProxy.GOOGLE_IPS)
    google_ips = sorted(GoAgentProxy.GOOGLE_IPS, key=lambda ip: GoAgentProxy.google_ip_failed_times.get(ip, 0))[:3]
    return sorted(google_ips, key=lambda ip: get_google_ip_latency(ip))[0]


def get_google_ip_latency(google_ip):
    if google_ip in GoAgentProxy.google_ip_latency_records:
        total_elapsed_seconds, times = GoAgentProxy.google_ip_latency_records[google_ip]
        return total_elapsed_seconds / times
    else:
        return 0


def record_google_ip_latency(google_ip, elapsed_seconds):
    if google_ip in GoAgentProxy.google_ip_latency_records:
        total_elapsed_seconds, times = GoAgentProxy.google_ip_latency_records[google_ip]
        total_elapsed_seconds += elapsed_seconds
        times += 1
        if times > 100:
            total_elapsed_seconds = total_elapsed_seconds / times
            times = 1
        GoAgentProxy.google_ip_latency_records[google_ip] = (total_elapsed_seconds, times)
    else:
        GoAgentProxy.google_ip_latency_records[google_ip] = (elapsed_seconds, 1)


class ConnectionFailed(Exception):
    pass


def http_call(ssl_sock, method, path, headers, payload):
    ssl_sock.settimeout(15)
    request_data = ''
    request_data += '%s %s HTTP/1.1\r\n' % (method, path)
    request_data += ''.join('%s: %s\r\n' % (k, v) for k, v in headers.items() if k not in SKIP_HEADERS)
    request_data += '\r\n'
    request_data = request_data.encode() + payload
    ssl_sock.counter.sending(len(request_data))
    ssl_sock.sendall(request_data)
    rfile = None
    counted_sock = None
    try:
        rfile = ssl_sock.makefile('rb', 0)
        counted_sock = CountedSock(rfile, ssl_sock.counter)
        response = http.client.HTTPResponse(counted_sock)
        response.ssl_sock = ssl_sock
        response.rfile = rfile
        response.counted_sock = counted_sock
        try:
            response.begin()
        except http.client.BadStatusLine:
            response = None
        ssl_sock.counter.received(len(counted_sock.rfile.captured))
        counted_sock.rfile.captured = ''
        return response
    except:
        for res in [ssl_sock, ssl_sock.sock, rfile, counted_sock]:
            try:
                if res:
                    res.close()
            except:
                pass
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.exception('failed to read goagent response')
        else:
            LOGGER.error('failed to read goagent response: %s' % sys.exc_info()[1])
        raise ReadResponseFailed()


class CountedSock(CapturingSock):
    def __init__(self, rfile, counter):
        super(CountedSock, self).__init__(rfile)
        self.counter = counter

    def close(self):
        self.counter.received(len(self.rfile.captured))


class ReadResponseFailed(Exception):
    pass


def gae_urlfetch(client, proxy, method, url, headers, payload, **kwargs):
    if payload:
        if len(payload) < 10 * 1024 * 1024 and 'Content-Encoding' not in headers:
            zpayload = zlib.compress(payload)[2:-4]
            if len(zpayload) < len(payload):
                payload = zpayload
                headers['Content-Encoding'] = 'deflate'
        headers['Content-Length'] = str(len(payload))
        # GAE donot allow set `Host` header
    if 'Host' in headers:
        del headers['Host']
    metadata = 'G-Method:%s\nG-Url:%s\n%s' % (
    method, url, ''.join('G-%s:%s\n' % (k, v) for k, v in kwargs.items() if v))
    metadata += ''.join('%s:%s\n' % (k.title(), v) for k, v in headers.items() if k not in SKIP_HEADERS)
    metadata = zlib.compress(metadata.encode())[2:-4]
    payload = b''.join((struct.pack('!h', len(metadata)), metadata, payload))
    ssl_sock = create_ssl_connection()
    ssl_sock.counter = stat.opened(ssl_sock, proxy, host=client.host, ip=client.dst_ip)
    LOGGER.info('[%s] urlfetch %s %s via %s %0.2f'
                % (repr(client), method, url, ssl_sock.google_ip, get_google_ip_latency(ssl_sock.google_ip)))
    client.add_resource(ssl_sock)
    client.add_resource(ssl_sock.counter)
    client.add_resource(ssl_sock.sock)
    response = http_call(ssl_sock, 'POST', proxy.fetch_server, {'Content-Length': str(len(payload))}, payload)
    client.add_resource(response.rfile)
    client.add_resource(response.counted_sock)
    response.app_status = response.status
    if response.status != 200:
        return response
    data = response.read(4)
    if len(data) < 4:
        response.status = 502
        response.fp = io.BytesIO(b'connection aborted. too short leadtype data=' + data)
        return response
    response.status, headers_length = struct.unpack('!hh', data)
    data = response.read(headers_length)
    if len(data) < headers_length:
        response.status = 502
        response.fp = io.BytesIO(b'connection aborted. too short headers data=' + data)
        return response
    response.headers = response.msg = http.client.parse_headers(io.BytesIO(zlib.decompress(data, -zlib.MAX_WBITS)))
    return response


class RangeFetch(object):
    def __init__(self, client, range_end, auto_ranged, response):
        self.client = client
        self.range_end = range_end
        self.auto_ranged = auto_ranged
        self.wfile = client.downstream_wfile
        self.response = response
        self.command = client.method
        self.url = client.url
        self.headers = client.headers
        self.payload = client.payload
        self._stopped = None

    def fetch(self):
        response_status = self.response.status
        response_headers = dict((k.title(), v) for k, v in self.response.getheaders())
        content_range = response_headers['Content-Range']
        LOGGER.info('auto ranged: %s' % self.auto_ranged)
        LOGGER.info('original response: %s' % content_range)
        #content_length = response_headers['Content-Length']
        start, end, length = list(map(int, re.search(r'bytes (\d+)-(\d+)/(\d+)', content_range).group(1, 2, 3)))
        if self.auto_ranged:
            response_status = 200
            response_headers.pop('Content-Range', None)
            response_headers['Content-Length'] = str(length)
        else:
            if self.range_end:
                response_headers['Content-Range'] = 'bytes %s-%s/%s' % (start, self.range_end, length)
                response_headers['Content-Length'] = str(self.range_end - start + 1)
            else:
                response_headers['Content-Range'] = 'bytes %s-%s/%s' % (start, length - 1, length)
                response_headers['Content-Length'] = str(length - start)

        if self.range_end:
            LOGGER.info('>>>>>>>>>>>>>>> RangeFetch started(%r) %d-%d', self.url, start, self.range_end)
        else:
            LOGGER.info('>>>>>>>>>>>>>>> RangeFetch started(%r) %d-end', self.url, start)
        general_resposne = ('HTTP/1.1 %s\r\n%s\r\n' % (
        response_status, ''.join('%s: %s\r\n' % (k, v) for k, v in response_headers.items()))).encode()
        LOGGER.info(general_resposne)
        self.wfile.write(general_resposne)

        data_queue = gevent.queue.PriorityQueue()
        range_queue = gevent.queue.PriorityQueue()
        range_queue.put((start, end, self.response))
        for begin in range(end + 1, self.range_end + 1 if self.range_end else length, AUTORANGE_MAXSIZE):
            range_queue.put((begin, min(begin + AUTORANGE_MAXSIZE - 1, length - 1), None))
        for i in range(AUTORANGE_THREADS):
            gevent.spawn(self.__fetchlet, range_queue, data_queue)
        has_peek = hasattr(data_queue, 'peek')
        peek_timeout = 90
        expect_begin = start
        while expect_begin < (self.range_end or (length - 1)):
            try:
                if has_peek:
                    begin, data = data_queue.peek(timeout=peek_timeout)
                    if expect_begin == begin:
                        data_queue.get()
                    elif expect_begin < begin:
                        gevent.sleep(0.1)
                        continue
                    else:
                        LOGGER.error('RangeFetch Error: begin(%r) < expect_begin(%r), quit.', begin, expect_begin)
                        break
                else:
                    begin, data = data_queue.get(timeout=peek_timeout)
                    if expect_begin == begin:
                        pass
                    elif expect_begin < begin:
                        data_queue.put((begin, data))
                        gevent.sleep(0.1)
                        continue
                    else:
                        LOGGER.error('RangeFetch Error: begin(%r) < expect_begin(%r), quit.', begin, expect_begin)
                        break
            except queue.Empty:
                LOGGER.error('data_queue peek timeout, break')
                break
            try:
                self.wfile.write(data)
                expect_begin += len(data)
            except (socket.error, ssl.SSLError, OSError) as e:
                LOGGER.info('RangeFetch client connection aborted(%s).', e)
                break
        self._stopped = True

    def __fetchlet(self, range_queue, data_queue):
        headers = copy.copy(self.headers)
        headers['Connection'] = 'close'
        while 1:
            try:
                if self._stopped:
                    return
                if data_queue.qsize() * AUTORANGE_BUFSIZE > 180 * 1024 * 1024:
                    gevent.sleep(10)
                    continue
                proxy = None
                try:
                    start, end, response = range_queue.get(timeout=1)
                    headers['Range'] = 'bytes=%d-%d' % (start, end)
                    if not response:
                        not_died_proxies = [p for p in GoAgentProxy.proxies if not p.died]
                        if not not_died_proxies:
                            self._stopped = True
                            return
                        proxy = random.choice(not_died_proxies)
                        response = gae_urlfetch(
                            self.client, proxy, self.command, self.url, headers, self.payload)
                except queue.Empty:
                    continue
                except (socket.error, ssl.SSLError, OSError, ConnectionFailed, ReadResponseFailed) as e:
                    LOGGER.warning("Response %r in __fetchlet", e)
                if not response:
                    LOGGER.warning('RangeFetch %s return %r', headers['Range'], response)
                    range_queue.put((start, end, None))
                    continue
                if response.app_status != 200:
                    LOGGER.warning('Range Fetch "%s %s" %s return %s', self.command, self.url, headers['Range'],
                                   response.app_status)
                    response.close()
                    range_queue.put((start, end, None))
                    if proxy:
                        proxy.died = True
                    continue
                if response.getheader('Location'):
                    self.url = response.getheader('Location')
                    LOGGER.info('RangeFetch Redirect(%r)', self.url)
                    response.close()
                    range_queue.put((start, end, None))
                    continue
                if 200 <= response.status < 300:
                    content_range = response.getheader('Content-Range')
                    if not content_range:
                        LOGGER.warning('RangeFetch "%s %s" return Content-Range=%r: response headers=%r', self.command,
                                       self.url, content_range, response.getheaders())
                        response.close()
                        range_queue.put((start, end, None))
                        continue
                    content_length = int(response.getheader('Content-Length', 0))
                    LOGGER.info('>>>>>>>>>>>>>>> [thread %s] %s %s', threading.currentThread().ident, content_length,
                                content_range)
                    while 1:
                        try:
                            data = response.read(AUTORANGE_BUFSIZE)
                            response.ssl_sock.counter.received(len(response.counted_sock.rfile.captured))
                            response.counted_sock.rfile.captured = ''
                            if not data:
                                break
                            data_queue.put((start, data))
                            start += len(data)
                        except (socket.error, ssl.SSLError, OSError) as e:
                            LOGGER.warning('RangeFetch "%s %s" %s failed: %s', self.command, self.url, headers['Range'],
                                           e)
                            break
                    if start < end:
                        LOGGER.warning('RangeFetch "%s %s" retry %s-%s', self.command, self.url, start, end)
                        response.close()
                        range_queue.put((start, end, None))
                        continue
                else:
                    LOGGER.error('RangeFetch %r return %s', self.url, response.status)
                    response.close()
                    range_queue.put((start, end, None))
                    continue
            except Exception as e:
                LOGGER.exception('RangeFetch._fetchlet error:%s', e)
                raise