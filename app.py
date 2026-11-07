#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# python-so-rebuild — sing-box/cloudflared/nezha 的 c-shared .so 加载版 Python 启动器
#
# 基线: EFI-rebuild/python/app.py
# 补丁:
#   1. pip 按需安装(.venv 已有依赖时跳过, 重启不依赖联网)
#   2. cleanup_files 保留 .so 缓存(重启零下载)
#   3. 不删 .tmp 目录(部分平台镜像 entrypoint 在 .tmp 下生成 CA bundle 并设
#      REQUESTS_CA_BUNDLE, 删掉会导致进程内所有 HTTPS 请求崩)
#   4. .so 下载源: github.com/guanxi660-crypto/python-so-rebuild releases
#      (sing-box v1.14.4 重编译版, 见 go-wrapper/)
# 变量一律走环境变量, 环境变量缺省时用下方默认值(占位)。
# ============================================================================

# 导入基础模块
import os, sys, subprocess, tempfile

# 屏蔽 quic-go 的 UDP 缓冲区警告（PaaS 容器无权限调大 rmem_max，该警告不影响功能）
os.environ.setdefault('QUIC_GO_DISABLE_RECEIVE_BUFFER_WARNING', 'true')

# 按需安装依赖（PyInstaller 冻结环境跳过——依赖已打包进 exe）：
# .venv 已有依赖（重启场景）时直接跳过，避免重启时 pip 联网失败导致 crash
if not getattr(sys, 'frozen', False):
    try:
        import requests  # noqa: F401
        import cryptography  # noqa: F401
    except ImportError:
        _pip_env = dict(os.environ)
        _pip_env.setdefault('PIP_CACHE_DIR', os.path.join(tempfile.gettempdir(), '.cache', 'pip'))
        subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "cryptography"], env=_pip_env)

# 导入其他依赖
import requests, re, ssl, json, time, base64, hashlib, secrets, shutil, signal, ctypes, threading
import platform, datetime
from typing import Optional
from ctypes import c_int, c_char_p
from cryptography.hazmat.primitives.asymmetric import x25519, ec
from cryptography.hazmat.primitives import serialization, hashes
import cryptography.x509 as x509
from cryptography.x509.oid import NameOID

# ======================== 环境变量定义 ========================
UPLOAD_URL = os.environ.get('UPLOAD_URL', '')     # 节点或订阅自动上传到订阅器的地址，需填写部署Merge-sub项目的首页，例如 https://merge.xxx.com
PROJECT_URL = os.environ.get('PROJECT_URL', '')   # 项目地址，例如：https://example.com,开启自动保活时或上传节点订阅需要填写
AUTO_ACCESS = os.environ.get('AUTO_ACCESS', 'false').lower() in ('true', 'yes') # 是否开启自动保活，true开启，false关闭，默认关闭
YT_WARPOUT = os.environ.get('YT_WARPOUT', 'false').lower() in ('true', 'yes')   # 是否开启youtube走warp出站，true开启，false关闭，默认关闭
FILE_PATH = os.environ.get('FILE_PATH', '.cache')  # 运行时文件存储路径，默认当前目录下的.cache文件夹

# 只读文件系统（如某些 PaaS 平台）上 cwd 不可写时，自动回退到可写的临时目录，
# 避免 start_server 里 makedirs('.cache') 报 OSError: [Errno 30] Read-only file system
def _resolve_writable_dir(_path):
    _candidate = _path if os.path.isabs(_path) else os.path.join(os.getcwd(), _path)
    try:
        os.makedirs(_candidate, exist_ok=True)
        _probe = os.path.join(_candidate, '.write_test')
        with open(_probe, 'w') as _f:
            _f.write('ok')
        os.unlink(_probe)
        return _candidate
    except Exception:
        _fallback = os.path.join(tempfile.gettempdir(), '.cache' if not os.path.isabs(_path) else os.path.basename(_path))
        os.makedirs(_fallback, exist_ok=True)
        return _fallback

FILE_PATH = _resolve_writable_dir(FILE_PATH)
SUB_PATH = os.environ.get('SUB_PATH', 'list')       # 获取订阅节点的token
UUID = os.environ.get('UUID', 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx') # 节点和哪吒v1使用的UUID，默认固定值，建议自行生成一个唯一的UUID
NEZHA_SERVER = os.environ.get('NEZHA_SERVER', 'nezha.ynotu.top:8008') # 哪吒面板域名,v1格式: nezha.xxx.com:8008  v0格式：nezha.xxx.com
NEZHA_KEY = os.environ.get('NEZHA_KEY', 'kabMIReszVfrjJUUyWLLiyJm2rzciO1j')       # 哪吒v1的NZ_CLIENT_SECRET的值，v0请的agent密钥
ARGO_PORT = int(os.environ.get('ARGO_PORT', '8888')) # 隧道端口,使用固定隧道token时需要在cloudflare里设置和这里一致
ARGO_DOMAIN = os.environ.get('ARGO_DOMAIN', '')  # 固定隧道域名，留空将使用临时隧道
ARGO_AUTH = os.environ.get('ARGO_AUTH', '')      # 固定密钥token或json，留空将使用临时隧道
S5_PORT = os.environ.get('S5_PORT', '')          # SOCKS5 端口，默认不启用
HY2_PORT = os.environ.get('HY2_PORT', '')        # Hysteria2 端口，默认不启用
TUIC_PORT = os.environ.get('TUIC_PORT', '')      # TUIC 端口，默认不启用
ANYTLS_PORT = os.environ.get('ANYTLS_PORT', '')  # AnyTLS 端口，默认不启用
REALITY_PORT = os.environ.get('REALITY_PORT', '') # Reality 端口，默认不启用
CFIP = os.environ.get('CFIP', 'spring.io')       # argo节点的优选域名或优选ip
CFPORT = int(os.environ.get('CFPORT', '443'))    # argo节点的优选域名或优选ip对应的端口
NAME = os.environ.get('NAME', '')               # 节点名称前缀
CHAT_ID = os.environ.get('CHAT_ID', '')         # Telegram机器人ID，例如1001234567890，关闭了log建议填写推送
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')     # Telegram机器人Token，例如123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
DISABLE_ARGO = os.environ.get('DISABLE_ARGO', 'false').lower() in ('true', 'yes') # 是否禁用Argo隧道，true/yes禁用，false/no启用，默认启用
SHOW_LOG = os.environ.get('SHOW_LOG', 'false').lower() not in ('false', 'disable', 'no')  # 是否显示日志输出，true/yes显示，false/disable/no屏蔽，默认显示
# ==============================================================

# 控制日志输出
def log(*args, **kwargs):
    if SHOW_LOG:
        print(*args, **kwargs)

# 全局变量
ROOT = os.getcwd()
runtimeFilePath = os.path.join(ROOT, FILE_PATH)
singBoxConfigPath = os.path.join(runtimeFilePath, 'config.json')
nezhaConfigPath = os.path.join(runtimeFilePath, 'config.yaml')
subPath = os.path.join(runtimeFilePath, 'debug.txt')
listPath = os.path.join(runtimeFilePath, 'list.txt')
keypairPath = os.path.join(runtimeFilePath, 'keypair.properties')

privateKey = ''
publicKey = ''

# 存储加载的库和回调
loaded_libs = {}
service_threads = {}

def get_arch():
    machine = platform.machine().lower()
    if machine in ('arm64', 'aarch64'):
        return 'arm64'
    return 'amd64'

ARCH = get_arch()

# ======================== 辅助函数 ========================

def is_valid_port(port):
    try:
        if port is None or port == '':
            return False
        port_num = int(port)
        return 1 <= port_num <= 65535
    except (ValueError, TypeError):
        return False

def sha256_file(filepath):
    sha256_hash = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for byte_block in iter(lambda: f.read(4096), b''):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

# ======================== 文件清理 ========================

paths_to_delete = ['list.txt', 'config.json', 'config.yaml', 'cert.pem', 'private.key', 'tunnel.json', 'tunnel.yml']

def cleanup_old_files():
    for file in paths_to_delete:
        filepath = os.path.join(FILE_PATH, file)
        try:
            if os.path.exists(filepath):
                os.unlink(filepath)
        except:
            pass

    # 注意：kata 镜像的 entrypoint 会在 .tmp/ 下生成 CA bundle（tmp.XXXX）并设置
    # REQUESTS_CA_BUNDLE 指向它，删掉 .tmp 会让本次进程内所有 requests HTTPS 请求
    # 直接崩（OSError: Could not find a suitable TLS CA certificate bundle）。
    # 因此这里绝不删 .tmp 目录。

def cleanup_files(keep_sub=False):
    # keep_files 必须保留下载的 .so 缓存，否则启动 120 秒后 delayed_cleanup
    # 把它们删掉，重启就得重新联网下载——源站或网络一抖就 crash
    keep_files = set(['keypair.properties', 'bot.so', 'tools.so', 'status.so'])
    if keep_sub:
        keep_files.add('debug.txt')

    if os.path.exists(runtimeFilePath):
        try:
            for file in os.listdir(runtimeFilePath):
                if file in keep_files:
                    continue
                filepath = os.path.join(runtimeFilePath, file)
                try:
                    if os.path.isdir(filepath):
                        shutil.rmtree(filepath)
                    else:
                        os.unlink(filepath)
                except:
                    pass
        except Exception as e:
            log(f'Cleanup failed: {e}')

    # 同 cleanup_old_files：.tmp 是镜像 entrypoint 的 CA bundle 所在地，不删
    # （120 秒后删掉会让运行中的进程下一次 HTTPS 请求崩掉）

def clear_console():
    os.system('clear' if os.name == 'posix' else 'cls')

def delete_nodes():
    try:
        if not UPLOAD_URL:
            return
        if not os.path.exists(subPath):
            return

        try:
            with open(subPath, 'r') as f:
                file_content = f.read()
        except:
            return

        decoded = base64.b64decode(file_content).decode('utf-8')
        nodes = [line for line in decoded.split('\n') 
                 if re.search(r'(vless|vmess|trojan|hysteria2|tuic):\/\/', line)]

        if not nodes:
            return

        try:
            requests.post(f'{UPLOAD_URL}/api/delete-nodes',
                         json={'nodes': nodes},
                         timeout=30)
        except:
            pass
    except Exception:
        pass

# ======================== Argo 隧道配置 ========================

def argo_type():
    if DISABLE_ARGO:
        log("DISABLE_ARGO is set to true, disable argo tunnel")
        return

    if not ARGO_AUTH or not ARGO_DOMAIN:
        return

    if 'TunnelSecret' in ARGO_AUTH:
        with open(os.path.join(FILE_PATH, 'tunnel.json'), 'w') as f:
            f.write(ARGO_AUTH)

        tunnel_id_match = re.search(r'"TunnelID":\s*"([^"]+)"', ARGO_AUTH)
        tunnel_id = tunnel_id_match.group(1) if tunnel_id_match else ""

        tunnel_yaml = f"""tunnel: {tunnel_id}
credentials-file: {os.path.join(FILE_PATH, 'tunnel.json')}
protocol: http2

ingress:
  - hostname: {ARGO_DOMAIN}
    service: http://localhost:{ARGO_PORT}
    originRequest:
      noTLSVerify: true
  - service: http_status:404
"""
        with open(os.path.join(FILE_PATH, 'tunnel.yml'), 'w') as f:
            f.write(tunnel_yaml)
    else:
        log(f"Using token connect to tunnel, please set {ARGO_PORT} in cloudflare")

# ======================== 下载库文件 ========================

# bot_{arch}.so 的 SHA-256，用于缓存校验与下载完整性验证（发版时同步更新）
# kata 专用：本地预置 sing-box v1.14.4 重编译版（go-wrapper/dist/bot_amd64_v1144.so），
# 不走 GitHub release 下载——download_library 见到缓存即用，本地文件替换后 hash 必须同步
BOT_SHA256 = {
    'amd64': 'bf04c21db41eb6ee2eed06c2bf4bb2eecd98dde517d898f80a3a09e5b58e1f0e',
    'arm64': '99375024f606cc9d3e331d232458d971464ead5a0e99c3898b8588ef813f1c0d',
}
# Windows 官方 DLL 构建产物的 SHA-256（发布新版时同步更新；留空则不做校验）
BOT_SHA256_DLL = {
    'amd64': '32e91515b4684535fa4752087d0f0e32d1337e7664db102ec11fa21d6012e91e',
    'arm64': None,   # 未构建，留空不做校验
}

def download_library(url: str, filename: str, expected_sha256: str = None) -> str:
    target = os.path.join(runtimeFilePath, filename)

    if os.path.exists(target):
        if expected_sha256 is None or sha256_file(target) == expected_sha256:
            log(f"Using cached native library: {target}")
            return target

    os.makedirs(runtimeFilePath, exist_ok=True)
    tmp = os.path.join(runtimeFilePath, f'{filename}.download')
    last_error = None
    for candidate_url in (url,):
        try:
            log(f"Downloading -> {target}")
            response = requests.get(candidate_url, stream=True, timeout=180)
            response.raise_for_status()
            with open(tmp, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            if expected_sha256 and sha256_file(tmp) != expected_sha256:
                raise Exception(f"SHA-256 mismatch for {tmp}")
            os.rename(tmp, target)
            os.chmod(target, 0o755)
            return target
        except Exception as error:
            last_error = error
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass
            if candidate_url == url:
                log(f"Primary download failed, trying fallback: {error}")
    raise last_error

def cloudflared_args():
    if DISABLE_ARGO:
        return None
    if ARGO_AUTH and ARGO_DOMAIN:
        if re.match(r'^[A-Z0-9a-z=]{120,250}$', ARGO_AUTH):
            return ['tunnel', '--edge-ip-version', 'auto', '--no-autoupdate',
                    '--protocol', 'http2', '--logfile', os.devnull,
                    '--loglevel', 'fatal', 'run', '--token', ARGO_AUTH]
        elif 'TunnelSecret' in ARGO_AUTH:
            return ['tunnel', '--edge-ip-version', 'auto', '--no-autoupdate',
                    '--logfile', os.devnull, '--loglevel', 'fatal',
                    '--config', os.path.join(FILE_PATH, 'tunnel.yml'), 'run']
    # ARGO_AUTH / ARGO_DOMAIN 未完整配置时静默跳过（不再走临时隧道）
    return None

def cloudflared_payload():
    args = cloudflared_args()
    return json.dumps({'args': args}) if args else None

def singbox_payload():
    return json.dumps({'config': singBoxConfigPath, 'workingDir': '.', 'disableColor': True})

def nezha_payload():
    return json.dumps({'config': nezhaConfigPath})


# ======================== 动态库加载 =========================

class NativeService:
    def __init__(self, name: str, lib_path: str, start_symbol: str, stop_symbol: str, payload: str):
        self.name = name
        self.lib_path = lib_path
        self.start_symbol = start_symbol
        self.stop_symbol = stop_symbol
        self.payload = payload
        self.lib = None
        self._stop_func = None
        self._running = False

    def start(self):
        """启动服务 - 在新线程中调用StartXXX函数"""
        try:
            # 加载动态库
            self.lib = ctypes.CDLL(self.lib_path)

            # 获取start函数
            start_func = getattr(self.lib, self.start_symbol)
            # 设置参数类型：const char*
            start_func.argtypes = [c_char_p]
            start_func.restype = c_int

            # 获取stop函数
            self._stop_func = getattr(self.lib, self.stop_symbol)
            self._stop_func.argtypes = []
            self._stop_func.restype = c_int

            # 在新线程中调用start函数（模拟异步）
            def run():
                try:
                    result = start_func(self.payload.encode('utf-8'))
                    if result != 0:
                        print(f"[ERROR] {self.name} native service exited with code {result}", flush=True)
                        if self.name == 'bot':
                            # bot 起不来：native 侧会留下空转的重试/监控协程占满一个核，
                            # 直接整进程退出，让面板拉起重试，而不是空占 CPU。
                            os._exit(1)
                except Exception as e:
                    print(f"[ERROR] {self.name} native service failed: {e}", flush=True)

            thread = threading.Thread(target=run, daemon=True, name=f"{self.name}-thread")
            thread.start()
            self._running = True

        except Exception as e:
            print(f"[ERROR] Failed to start {self.name}: {e}", flush=True)
            raise

    def stop(self):
        """停止服务（最多等待 5 秒，超时即跳过，由退出兜底）"""
        if not self._running or self._stop_func is None:
            return

        result = {}
        def _call():
            try:
                result['code'] = self._stop_func()
            except Exception as e:
                result['error'] = e

        t = threading.Thread(target=_call, daemon=True, name=f"{self.name}-stop")
        t.start()
        t.join(timeout=5)
        if t.is_alive():
            log(f"{self.name} stop timed out after 5s, skipping")
            return
        if 'code' in result:
            self._running = False
            log(f"{self.name} stopped with code {result['code']}")
        else:
            log(f"Failed to stop {self.name}: {result.get('error')}")

# ======================== Reality X25519 密钥对 ========================

def clamp_x25519_private_key(private_key: bytes) -> bytes:
    if len(private_key) != 32:
        raise ValueError('X25519 private key must be 32 bytes')
    key = bytearray(private_key)
    key[0] &= 248
    key[31] &= 127
    key[31] |= 64
    return bytes(key)

def base64url_no_padding(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')

def decode_base64url_no_padding(value: str) -> bytes:
    value = value.strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('invalid base64url value')
    padding = '=' * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)

def x25519_pure_python(private_key: bytes, public_key: bytes) -> bytes:
    P = 2**255 - 19
    A24 = 121665

    def decode_scalar(k):
        return clamp_x25519_private_key(k)

    def decode_int(s):
        return sum(s[i] << (8 * i) for i in range(32))

    def encode_int(n):
        return bytes((n >> (8 * i)) & 0xff for i in range(32))

    def cswap(swap, x2, x3):
        dummy = swap * (x2 - x3)
        x2 -= dummy
        x3 += dummy
        return x2, x3

    k = decode_scalar(private_key)
    u = decode_int(public_key)

    x1 = u
    x2 = 1
    z2 = 0
    x3 = x1
    z3 = 1
    swap = 0

    for t in range(254, -1, -1):
        k_t = (k[t // 8] >> (t % 8)) & 1
        swap ^= k_t
        x2, x3 = cswap(swap, x2, x3)
        z2, z3 = cswap(swap, z2, z3)
        swap = k_t

        A = (x2 + z2) % P
        AA = (A * A) % P
        B = (x2 - z2) % P
        BB = (B * B) % P
        E = (AA - BB) % P
        C = (x3 + z3) % P
        D = (x3 - z3) % P
        DA = (D * A) % P
        CB = (C * B) % P
        x3 = ((DA + CB) * (DA + CB)) % P
        z3 = (x1 * ((DA - CB) * (DA - CB) % P)) % P
        x2 = (AA * BB) % P
        z2 = (E * ((AA + (A24 * E) % P) % P)) % P

    x2, x3 = cswap(swap, x2, x3)
    z2, z3 = cswap(swap, z2, z3)

    inv_z2 = pow(z2, P-2, P)
    result = (x2 * inv_z2) % P

    return encode_int(result)

def derive_x25519_public_key(private_key_bytes: bytes) -> bytes:
    private_key_bytes = clamp_x25519_private_key(private_key_bytes)
    private_key = x25519.X25519PrivateKey.from_private_bytes(private_key_bytes)
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )

def generate_reality_keypair():
    private_bytes = clamp_x25519_private_key(secrets.token_bytes(32))
    public_bytes = derive_x25519_public_key(private_bytes)
    return {
        'privateKey': base64url_no_padding(private_bytes),
        'publicKey': base64url_no_padding(public_bytes)
    }

def write_keypair(private_key_value: str, public_key_value: str):
    os.makedirs(os.path.dirname(keypairPath), exist_ok=True)
    with open(keypairPath, 'w') as f:
        f.write(f'PrivateKey: {private_key_value}\nPublicKey: {public_key_value}\n')

def generate_or_load_keypair():
    global privateKey, publicKey

    if os.path.exists(keypairPath):
        with open(keypairPath, 'r') as f:
            content = f.read()
        private_match = re.search(r'PrivateKey:\s*(.*)', content)
        public_match = re.search(r'PublicKey:\s*(.*)', content)
        if private_match and public_match:
            try:
                loaded_private = decode_base64url_no_padding(private_match.group(1))
                loaded_public = decode_base64url_no_padding(public_match.group(1))
                normalized_private = clamp_x25519_private_key(loaded_private)
                derived_public = derive_x25519_public_key(normalized_private)
                if len(loaded_public) != 32 or derived_public != loaded_public:
                    raise ValueError('stored public key does not match private key')
                privateKey = base64url_no_padding(normalized_private)
                publicKey = base64url_no_padding(derived_public)
                if privateKey != private_match.group(1).strip() or publicKey != public_match.group(1).strip():
                    write_keypair(privateKey, publicKey)
                log(f'Private Key: {privateKey}')
                log(f'Public Key: {publicKey}')
                return
            except Exception as e:
                log(f'Invalid Reality keypair, regenerating: {e}')

    pair = generate_reality_keypair()
    privateKey = pair['privateKey']
    publicKey = pair['publicKey']
    write_keypair(privateKey, publicKey)
    log(f'Private Key: {privateKey}')
    log(f'Public Key: {publicKey}')

# ======================== TLS 证书 ========================

FALLBACK_EC_KEY = '''-----BEGIN EC PARAMETERS-----
BggqhkjOPQMBBw==
-----END EC PARAMETERS-----
-----BEGIN EC PRIVATE KEY-----
MHcCAQEEIM4792SEtPqIt1ywqTd/0bYidBqpYV/++siNnfBYsdUYoAoGCCqGSM49
AwEHoUQDQgAE1kHafPj07rJG+HboH2ekAI4r+e6TL38GWASANnngZreoQDF16ARa
/TsyLyFoPkhLxSbehH/NBEjHtSZGaDhMqQ==
-----END EC PRIVATE KEY-----
'''

FALLBACK_CERT = '''-----BEGIN CERTIFICATE-----
MIIBejCCASGgAwIBAgIUfWeQL3556PNJLp/veCFxGNj9crkwCgYIKoZIzj0EAwIw
EzERMA8GA1UEAwwIYmluZy5jb20wHhcNMjUwOTE4MTgyMDIyWhcNMzUwOTE2MTgy
MDIyWjATMREwDwYDVQQDDAhiaW5nLmNvbTBZMBMGByqGSM49AgEGCCqGSM49AwEH
A0IABNZB2nz49O6yRvh26B9npACOK/nuky9/BlgEgDZ54Ga3qEAxdegEWv07Mi8h
aD5IS8Um3oR/zQRIx7UmRmg4TKmjUzBRMB0GA1UdDgQWBBTV1cFID7UISE7PLTBR
BfGbgkrMNzAfBgNVHSMEGDAWgBTV1cFID7UISE7PLTBRBfGbgkrMNzAPBgNVHRMB
Af8EBTADAQH/MAoGCCqGSM49BAMCA0cAMEQCIAIDAJvg0vd/ytrQVvEcSm6XTlB+
eQ6OFb9LbLYL9f+sAiAffoMbi4y/0YUSlTtz7as9S8/lciBF5VCUoVIKS+vX2g==
-----END CERTIFICATE-----
'''

def ensure_tls_certificates(cert_path: str, key_path: str):
    if os.path.exists(cert_path) and os.path.exists(key_path) and tls_certificate_pair_is_valid(cert_path, key_path):
        return

    os.makedirs(os.path.dirname(cert_path), exist_ok=True)
    temp_cert_path = f'{cert_path}.tmp'
    temp_key_path = f'{key_path}.tmp'
    for temp_path in (temp_cert_path, temp_key_path):
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except:
            pass

    def _openssl_generate():
        subprocess.run(['openssl', 'version'], capture_output=True, check=True)
        subprocess.run([
            'openssl', 'ecparam', '-genkey', '-name', 'prime256v1', '-out', temp_key_path
        ], capture_output=True, check=True)
        subprocess.run([
            'openssl', 'req', '-new', '-x509', '-days', '3650',
            '-key', temp_key_path, '-out', temp_cert_path, '-subj', '/CN=bing.com'
        ], capture_output=True, check=True)

    def _crypto_generate():
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'bing.com')])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(subject).issuer_name(subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now)
                .not_valid_after(now + datetime.timedelta(days=3650))
                .sign(key, hashes.SHA256()))
        with open(temp_key_path, 'wb') as f:
            f.write(key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.TraditionalOpenSSL,
                                      serialization.NoEncryption()))
        with open(temp_cert_path, 'wb') as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

    generated = False
    try:
        _openssl_generate()
        generated = True
    except Exception:
        try:
            _crypto_generate()
            generated = True
        except Exception:
            generated = False

    if generated:
        try:
            if tls_certificate_pair_is_valid(temp_cert_path, temp_key_path):
                os.replace(temp_cert_path, cert_path)
                os.replace(temp_key_path, key_path)
                return
        except Exception:
            pass

    for temp_path in (temp_cert_path, temp_key_path):
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except:
            pass

    with open(key_path, 'w') as f:
        f.write(FALLBACK_EC_KEY)
    with open(cert_path, 'w') as f:
        f.write(FALLBACK_CERT)
    if not tls_certificate_pair_is_valid(cert_path, key_path):
        raise RuntimeError('failed to create a valid TLS certificate pair')

def tls_certificate_pair_is_valid(cert_path: str, key_path: str) -> bool:
    if not os.path.exists(cert_path) or not os.path.exists(key_path):
        return False
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        return True
    except Exception:
        return False

# ======================== sing-box 配置生成 ========================

def generate_singbox_config(cert_path: str, key_path: str) -> dict:
    inbounds = []

    inbounds.append({
        'type': 'vless',
        'tag': 'vless-argo-in',
        'listen': '::',
        'listen_port': ARGO_PORT,
        'users': [{'uuid': UUID}],
        'transport': {
            'type': 'ws',
            'path': '/vless-argo',
            'early_data_header_name': 'Sec-WebSocket-Protocol'
        }
    })

    if is_valid_port(REALITY_PORT):
        inbounds.append({
            'type': 'vless',
            'tag': 'vless-reality',
            'listen': '::',
            'listen_port': int(REALITY_PORT),
            'users': [{'uuid': UUID, 'flow': 'xtls-rprx-vision'}],
            'tls': {
                'enabled': True,
                'server_name': 'www.iij.ad.jp',
                'reality': {
                    'enabled': True,
                    'handshake': {'server': 'www.iij.ad.jp', 'server_port': 443},
                    'private_key': privateKey,
                    'short_id': ['']
                }
            }
        })

    if is_valid_port(HY2_PORT):
        inbounds.append({
            'type': 'hysteria2',
            'tag': 'hysteria-in',
            'listen': '::',
            'listen_port': int(HY2_PORT),
            'users': [{'password': UUID}],
            'masquerade': 'https://bing.com',
            'tls': {
                'enabled': True,
                'alpn': ['h3'],
                'certificate_path': cert_path,
                'key_path': key_path
            }
        })

    if is_valid_port(TUIC_PORT):
        inbounds.append({
            'type': 'tuic',
            'tag': 'tuic-in',
            'listen': '::',
            'listen_port': int(TUIC_PORT),
            'users': [{'uuid': UUID, 'password': UUID}],
            'congestion_control': 'bbr',
            'tls': {
                'enabled': True,
                'alpn': ['h3'],
                'certificate_path': cert_path,
                'key_path': key_path
            }
        })

    if is_valid_port(S5_PORT):
        inbounds.append({
            'type': 'socks',
            'tag': 's5-in',
            'listen': '::',
            'listen_port': int(S5_PORT),
            'users': [{
                'username': UUID[:8],
                'password': UUID[-12:]
            }]
        })

    if is_valid_port(ANYTLS_PORT):
        inbounds.append({
            'type': 'anytls',
            'tag': 'anytls-in',
            'listen': '::',
            'listen_port': int(ANYTLS_PORT),
            'users': [{'password': UUID}],
            'tls': {
                'enabled': True,
                'certificate_path': cert_path,
                'key_path': key_path
            }
        })

    endpoints = [{
        'type': 'wireguard',
        'tag': 'wireguard-out',
        'mtu': 1280,
        'address': ['172.16.0.2/32', '2606:4700:110:8dfe:d141:69bb:6b80:925/128'],
        'private_key': 'YFYOAdbw1bKTHlNNi+aEjBM3BO7unuFC5rOkMRAz9XY=',
        'peers': [{
            'address': 'engage.cloudflareclient.com',
            'port': 2408,
            'public_key': 'bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=',
            'allowed_ips': ['0.0.0.0/0', '::/0'],
            'reserved': [78, 135, 76]
        }]
    }]

    rule_set = [
        {'tag': 'netflix', 'type': 'remote', 'format': 'binary',
         'url': 'https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/sing/geo/geosite/netflix.srs'}
    ]
    wireguard_rule_sets = ['netflix']

    need_youtube_warp = YT_WARPOUT
    if not need_youtube_warp:
        try:
            result = subprocess.run(
                ['curl', '-o', os.devnull, '-m', '2', '-s', '-w', '%{http_code}',
                 'https://www.youtube.com'],
                capture_output=True, text=True, timeout=5
            )
            need_youtube_warp = result.stdout.strip() != '200'
        except:
            need_youtube_warp = True

    if need_youtube_warp:
        rule_set.append({
            'tag': 'youtube', 'type': 'remote', 'format': 'binary',
            'url': 'https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/sing/geo/geosite/youtube.srs'
        })
        wireguard_rule_sets.append('youtube')
        log('Add YouTube outbound rule')

    route = {
        'default_http_client': 'http-client-direct',
        'rule_set': rule_set,
        'rules': [{'rule_set': wireguard_rule_sets, 'outbound': 'wireguard-out'}],
        'final': 'direct'
    }

    return {
        'log': {'disabled': True, 'level': 'error', 'timestamp': True},
        'http_clients': [{'tag': 'http-client-direct'}],
        'inbounds': inbounds,
        'endpoints': endpoints,
        'outbounds': [{'type': 'direct', 'tag': 'direct'}],
        'route': route
    }

def generate_nezha_config():
    nzport = NEZHA_SERVER.split(':')[-1] if ':' in NEZHA_SERVER else ''
    tls_ports = {'443', '8443', '2096', '2087', '2083', '2053'}
    nezhatls = 'true' if nzport in tls_ports else 'false'

    config_yaml = f'''client_secret: {NEZHA_KEY}
debug: false
disable_auto_update: true
disable_command_execute: false
disable_force_update: true
disable_nat: false
disable_send_query: false
gpu: false
insecure_tls: true
ip_report_period: 1800
report_delay: 4
server: {NEZHA_SERVER}
skip_connection_count: true
skip_procs_count: true
temperature: false
tls: {nezhatls}
use_gitee_to_upgrade: false
use_ipv6_country_code: false
uuid: {UUID}'''

    with open(nezhaConfigPath, 'w') as f:
        f.write(config_yaml)

# ======================== 隧道域名检测 ========================

def extract_domain() -> Optional[str]:
    if DISABLE_ARGO:
        return None
    if ARGO_AUTH and ARGO_DOMAIN:
        log(f'ARGO_DOMAIN: {ARGO_DOMAIN}')
        return ARGO_DOMAIN

    # ARGO_AUTH / ARGO_DOMAIN 未完整配置：静默返回，不打日志
    return None

# ======================== ISP 信息 ========================

def get_meta_info() -> str:
    try:
        response = requests.get('https://api.ip.sb/geoip', timeout=3)
        if response.status_code == 200:
            data = response.json()
            if data.get('country_code') and data.get('isp'):
                return f"{data['country_code']}-{data['isp']}".replace(' ', '_')
    except:
        pass

    try:
        response = requests.get('http://ip-api.com/json', timeout=3)
        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'success' and data.get('countryCode') and data.get('org'):
                return f"{data['countryCode']}-{data['org']}".replace(' ', '_')
    except:
        pass

    return 'Unknown'

# ======================== 节点链接生成 ========================

def get_server_ip() -> str:
    try:
        response = requests.get('http://ipv4.ip.sb', timeout=3)
        if response.status_code == 200:
            return response.text.strip()
    except:
        pass

    try:
        result = subprocess.run(['curl', '-sm', '3', 'ipv4.ip.sb'], 
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except:
        pass

    try:
        response = requests.get('http://ipv6.ip.sb', timeout=3)
        if response.status_code == 200:
            return f"[{response.text.strip()}]"
    except:
        pass

    try:
        result = subprocess.run(['curl', '-sm', '3', 'ipv6.ip.sb'],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return f"[{result.stdout.strip()}]"
    except:
        pass

    return ""

def generate_links(argo_domain: Optional[str]) -> str:
    server_ip = get_server_ip()
    isp = get_meta_info()
    node_name = f"{NAME}-{isp}" if NAME else isp

    time.sleep(2)

    sub_txt = ''

    if not DISABLE_ARGO and argo_domain:
        vless_node = f"vless://{UUID}@{CFIP}:{CFPORT}?type=ws&security=tls&path=%2Fvless-argo%3Fed%3D2560&host={argo_domain}&sni={argo_domain}&fp=firefox#{node_name}"
        sub_txt = vless_node

    if is_valid_port(TUIC_PORT):
        sub_txt += f"\ntuic://{UUID}:{UUID}@{server_ip}:{TUIC_PORT}?sni=www.bing.com&congestion_control=bbr&udp_relay_mode=native&alpn=h3&allow_insecure=1#{node_name}"

    if is_valid_port(HY2_PORT):
        sub_txt += f"\nhysteria2://{UUID}@{server_ip}:{HY2_PORT}/?sni=www.bing.com&insecure=1&alpn=h3&obfs=none#{node_name}"

    if is_valid_port(REALITY_PORT):
        sub_txt += f"\nvless://{UUID}@{server_ip}:{REALITY_PORT}?encryption=none&flow=xtls-rprx-vision&security=reality&sni=www.iij.ad.jp&fp=firefox&pbk={publicKey}&type=tcp&headerType=none#{node_name}"

    if is_valid_port(ANYTLS_PORT):
        sub_txt += f"\nanytls://{UUID}@{server_ip}:{ANYTLS_PORT}?security=tls&sni={server_ip}&fp=chrome&insecure=1&allowInsecure=1#{node_name}"

    if is_valid_port(S5_PORT):
        s5_auth = base64.b64encode(f"{UUID[:8]}:{UUID[-12:]}".encode()).decode()
        sub_txt += f"\nsocks://{s5_auth}@{server_ip}:{S5_PORT}#{node_name}"

    encoded = base64.b64encode(sub_txt.encode()).decode()
    log(f'\033[32m{encoded}\033[0m')
    log('\033[35mLogs will be deleted in 120 seconds, you can copy the above nodes\033[0m')

    with open(subPath, 'w') as f:
        f.write(base64.b64encode(sub_txt.encode()).decode())
    with open(listPath, 'w') as f:
        f.write(sub_txt)

    log(f'{FILE_PATH}/debug.txt saved successfully')
    return sub_txt

# ======================== Telegram 推送 ========================

def send_telegram():
    if not BOT_TOKEN or not CHAT_ID:
        log('TG variables is empty, Skipping push nodes to TG')
        return

    try:
        with open(subPath, 'r') as f:
            message = f.read()

        escaped_name = re.sub(r'([_*[\]()~`>#+=|{}.!-])', r'\\\1', NAME)
        text = f"**{escaped_name}节点推送通知**\n```{message}```"

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        params = {
            'chat_id': CHAT_ID,
            'text': text,
            'parse_mode': 'MarkdownV2'
        }
        requests.post(url, params=params, timeout=30)
        log('Telegram message sent successfully')
    except Exception as error:
        log(f'Failed to send Telegram message: {error}')

# ======================== 节点上传 ========================

def upload_nodes():
    if UPLOAD_URL and PROJECT_URL:
        subscription_url = f"{PROJECT_URL}/{SUB_PATH}"
        json_data = {'subscription': [subscription_url]}
        try:
            response = requests.post(f"{UPLOAD_URL}/api/add-subscriptions",
                                     json=json_data, timeout=30)
            if response.status_code == 200:
                log('Subscription uploaded successfully')
        except:
            pass
    elif UPLOAD_URL:
        if not os.path.exists(listPath):
            return
        with open(listPath, 'r') as f:
            content = f.read()
        nodes = [line for line in content.split('\n')
                 if re.search(r'(vless|vmess|trojan|hysteria2|tuic):\/\/', line)]
        if not nodes:
            return
        try:
            response = requests.post(f"{UPLOAD_URL}/api/add-nodes",
                                     json={'nodes': nodes}, timeout=30)
            if response.status_code == 200:
                log('Subscription uploaded successfully')
        except:
            pass

# ======================== 自动保活 ========================

def add_visit_task():
    if not AUTO_ACCESS or not PROJECT_URL:
        log('Skipping adding automatic access task')
        return

    try:
        requests.post('https://oooo.serv00.net/add-url',
                      json={'url': PROJECT_URL}, timeout=30)
        log('Automatic access task added successfully')
    except Exception as error:
        log(f'Add URL failed: {error}')

# ======================== 主流程 ========================

def start_server():
    global privateKey, publicKey

    # 1. 删除旧节点
    delete_nodes()

    # 2. 创建运行目录 + 清理文件
    if not os.path.exists(FILE_PATH):
        os.makedirs(FILE_PATH, exist_ok=True)
        log(f'{FILE_PATH} is created')
    cleanup_old_files()

    # 3. 生成 Argo 隧道配置
    argo_type()

    # 4. 下载库文件
    lib_ext = 'dll' if os.name == 'nt' else 'so'
    base_url = f'https://github.com/guanxi660-crypto/EFI-rebuild/releases/latest/download'
    bot_expected_sha = BOT_SHA256_DLL.get(ARCH) if os.name == 'nt' else BOT_SHA256.get(ARCH)
    bot_lib = download_library(f'{base_url}/bot_{ARCH}.{lib_ext}', f'bot.{lib_ext}',
                               bot_expected_sha)

    tools_lib = None
    status_lib = None

    if not DISABLE_ARGO and ARGO_AUTH and ARGO_DOMAIN:
        tools_lib = download_library(f'{base_url}/tools_{ARCH}.{lib_ext}', f'tools.{lib_ext}')

    if NEZHA_SERVER and NEZHA_KEY:
        status_lib = download_library(f'{base_url}/status_{ARCH}.{lib_ext}', f'status.{lib_ext}')
    else:
        log('NEZHA variable is empty, skipping')

    # 5. 生成 Reality 密钥对
    if REALITY_PORT:
        generate_or_load_keypair()

    # 6. 生成 TLS 证书
    cert_path = os.path.join(FILE_PATH, 'cert.pem')
    key_path = os.path.join(FILE_PATH, 'private.key')
    needs_tls = bool(HY2_PORT or TUIC_PORT or ANYTLS_PORT)
    if needs_tls:
        ensure_tls_certificates(cert_path, key_path)

    # 7. 生成 nezha config
    if NEZHA_SERVER and NEZHA_KEY:
        generate_nezha_config()

    # 8. 生成 sing-box config.json
    sbx_config = generate_singbox_config(cert_path, key_path)
    with open(singBoxConfigPath, 'w') as f:
        json.dump(sbx_config, f, indent=2)


    # 9. 创建并启动服务
    services = []

    # bot服务
    bot_service = NativeService(
        'bot', bot_lib,
        'StartBot', 'StopBot',
        singbox_payload()
    )
    services.append(bot_service)

    # tools服务
    tools_service = None
    if tools_lib:
        cf_payload = cloudflared_payload()
        if cf_payload:
            tools_service = NativeService(
                'tools', tools_lib,
                'StartTools', 'StopTools',
                cf_payload
            )
            services.append(tools_service)

    # status服务
    status_service = None
    if status_lib:
        status_service = NativeService(
            'status', status_lib,
            'StartStatus', 'StopStatus',
            nezha_payload()
        )
        services.append(status_service)


    # 信号处理
    def stop_all():
        log("\nStopping all services...")
        for service in reversed(services):
            try:
                service.stop()
            except:
                pass
        sys.stdout.flush()
        os._exit(0)

    signal.signal(signal.SIGINT, lambda s, f: stop_all())
    signal.signal(signal.SIGTERM, lambda s, f: stop_all())

    # 启动所有服务
    for service in services:
        service.start()

    time.sleep(3)
    log('web is running')
    if tools_service:
        log('tools is running')
    if status_service:
        log('status is running')

    # 10. 等待并检测隧道域名
    time.sleep(5)
    argo_domain = extract_domain()

    # 11. 生成节点链接（写入 debug.txt/list.txt，供 Telegram 推送与上传使用）
    generate_links(argo_domain)

    # 12. Telegram 推送 + 节点上传
    send_telegram()
    upload_nodes()
    add_visit_task()

    # 13. 120秒后清理文件 + 清屏
    def delayed_cleanup():
        time.sleep(120)
        cleanup_files(keep_sub=True)
        clear_console()
        print('App is running')

    cleanup_thread = threading.Thread(target=delayed_cleanup, daemon=True)
    cleanup_thread.start()

    # 保持主线程运行
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_all()

if __name__ == '__main__':
    start_server()