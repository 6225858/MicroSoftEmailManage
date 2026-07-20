"""
代理池服务：轮询调度 + 自动检测 + HTTP/SOCKS5 支持。
"""
import logging
import threading
import time
import urllib.parse

import requests
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

import json
import socket

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 通过代理访问的目标测试地址
PROXY_TEST_URLS = [
    "https://login.microsoftonline.com/common/.well-known/openid-configuration",
    "https://www.google.com/generate_204",
    "https://httpbin.org/ip",
]
PROXY_TEST_TIMEOUT = 8
PROXY_TCP_TIMEOUT = 5
# IP 信息批量查询（一次请求查全部，最多 100 个 IP）
IP_BATCH_URL = "http://ip-api.com/batch?fields=country,regionName,city,isp,org,proxy,hosting,mobile"
IP_BATCH_MAX = 100
SOCKS5_AVAILABLE = False

try:
    import socks  # noqa: F401
    SOCKS5_AVAILABLE = True
except ImportError:
    pass

_round_index = 0
_round_lock = threading.Lock()


def _build_proxy_url(proxy) -> str:
    """构建 requests 库用的代理 URL。"""
    scheme = proxy.proxy_type
    if scheme == "socks5":
        scheme = "socks5h" if SOCKS5_AVAILABLE else "socks5"

    if proxy.username and proxy.password:
        user = urllib.parse.quote(proxy.username, safe="")
        pwd = urllib.parse.quote(proxy.password, safe="")
        return f"{scheme}://{user}:{pwd}@{proxy.host}:{proxy.port}"
    return f"{scheme}://{proxy.host}:{proxy.port}"


def import_proxy_line(line: str) -> dict | None:
    """
    解析一行代理配置，支持格式：
      ip:port
      ip:port:username:password          ← 新增
      protocol://ip:port
      protocol://user:pass@ip:port
      user:pass@ip:port
    """
    line = line.strip()
    if not line:
        return None

    try:
        # 格式: protocol://user:pass@host:port
        if "://" in line:
            parsed = urllib.parse.urlparse(line)
            proxy_type = parsed.scheme or "http"
            host = parsed.hostname
            port = parsed.port or 1080
            username = parsed.username or ""
            password = parsed.password or ""
        else:
            # 按冒号拆分段数来判断格式
            parts = line.rsplit(":", 3)  # host:port:user:pass 最多 4 段

            if len(parts) == 4 and "@" not in parts[0]:
                # host:port:username:password（host 段不含 @）
                host = parts[0]
                port = int(parts[1])
                username = parts[2]
                password = parts[3]
                proxy_type = "http"
            elif len(parts) == 2:
                # host:port
                host = parts[0]
                port = int(parts[1])
                proxy_type = "http"
                username = ""
                password = ""
            elif "@" in line and "://" not in line:
                # user:pass@host:port
                auth, hostport = line.rsplit("@", 1)
                if ":" in auth:
                    username, password = auth.split(":", 1)
                else:
                    username, password = auth, ""
                host, port_str = hostport.rsplit(":", 1)
                port = int(port_str)
                proxy_type = "http"
            else:
                return None

        if not host or not port:
            return None

        # 限制端口范围
        if port < 1 or port > 65535:
            return None

        return {
            "name": f"{host}:{port}",
            "proxy_type": proxy_type,
            "host": host,
            "port": port,
            "username": username,
            "password": password,
        }
    except Exception:
        return None


def _tcp_test(host: str, port: int, timeout: int = PROXY_TCP_TIMEOUT) -> bool:
    """TCP 连接测试：代理服务器本身是否可达。"""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False


def _calc_purity(info: dict) -> str:
    """根据 IP 信息计算纯净度 JSON。"""
    isp = info.get("isp", "")
    org = info.get("org", "")
    is_proxy = info.get("proxy", False)
    is_hosting = info.get("hosting", False)
    is_mobile = info.get("mobile", False)
    country = info.get("country", "")
    city = info.get("city", "")

    if is_mobile:
        ip_type, level = "移动网络", "高"
    elif is_hosting:
        ip_type, level = "机房/托管", "低"
    elif "residential" in (isp + org).lower() or "broadband" in (isp + org).lower():
        ip_type, level = "住宅宽带", "高"
    elif is_proxy:
        ip_type, level = "已知代理", "低"
    elif isp:
        ip_type, level = "商业ISP", "中"
    else:
        ip_type, level = "未知", "中"

    return json.dumps({
        "level": level, "label": ip_type,
        "isp": isp or org or "", "country": country, "city": city,
    }, ensure_ascii=False)


def test_proxy(proxy, timeout: int = PROXY_TEST_TIMEOUT) -> tuple[bool, int, str]:
    """
    快速检测：TCP 可达性 + HTTP 延迟 + 出口 IP。
    不查询纯净度（纯净度在 test_proxies_status 中统一批量查）。

    返回: (是否可用, 延迟ms, 出口IP)
    """
    if proxy.proxy_type == "socks5" and not SOCKS5_AVAILABLE:
        return (False, 0, "")

    tcp_start = time.time()
    tcp_ok = _tcp_test(proxy.host, proxy.port)
    tcp_latency = int((time.time() - tcp_start) * 1000)

    if not tcp_ok:
        return (False, 0, "")

    proxy_url = _build_proxy_url(proxy)
    proxies = {"http": proxy_url, "https": proxy_url}
    exit_ip = ""
    last_error = ""

    for test_url in PROXY_TEST_URLS:
        try:
            resp = requests.get(
                test_url,
                proxies=proxies,
                timeout=timeout,
                verify=False,  # 部分代理有 SSL 证书问题
            )
            if resp.ok:
                if "httpbin" in test_url:
                    try:
                        data = resp.json()
                        exit_ip = data.get("origin", "").split(",")[0].strip()
                    except Exception:
                        pass
                logger.debug("代理 %s:%s HTTP 测试成功: %s", proxy.host, proxy.port, test_url)
                break
        except requests.exceptions.SSLError:
            last_error = "SSL证书错误"
            continue
        except requests.exceptions.ProxyError as e:
            last_error = f"代理连接被拒: {e}"
            continue
        except requests.exceptions.ConnectTimeout:
            last_error = "连接超时"
            continue
        except requests.exceptions.ReadTimeout:
            last_error = "读取超时"
            continue
        except Exception as e:
            last_error = str(e)[:100]
            continue

    if not exit_ip and last_error:
        logger.warning("代理 %s:%s 未获取出口IP, 错误: %s", proxy.host, proxy.port, last_error)

    return (True, tcp_latency, exit_ip)


def _batch_query_purity(ips: list[str]) -> dict[str, str]:
    """
    批量查询 IP 纯净度。一次 HTTP 请求查最多 100 个 IP。
    返回 {ip: purity_json}
    """
    result = {}
    if not ips:
        return result

    try:
        resp = requests.post(
            IP_BATCH_URL,
            json=[{"query": ip, "fields": "country,regionName,city,isp,org,proxy,hosting,mobile"}
                  for ip in ips[:IP_BATCH_MAX]],
            timeout=15,
        )
        if resp.ok:
            data = resp.json()
            for item in data:
                query_ip = item.get("query", "")
                if query_ip:
                    result[query_ip] = _calc_purity(item)
    except Exception:
        logger.warning("批量 IP 纯净度查询失败")

    return result


def test_proxies_status(db: Session) -> None:
    """检测所有代理：快速测试 + 统一批量查纯净度。"""
    from models import Proxy
    proxies = db.query(Proxy).all()

    # 第一步：逐个快速检测代理（TCP + HTTP 延迟 + IP）
    ips_to_check = []
    success = fail = 0
    for proxy in proxies:
        ok, latency, ip = test_proxy(proxy)
        proxy.status = 1 if ok else 0
        proxy.latency_ms = latency
        proxy.exit_ip = ip
        proxy.last_checked_at = int(time.time())
        if ok:
            success += 1
            if ip and ip not in ips_to_check:
                ips_to_check.append(ip)
        else:
            fail += 1
            proxy.purity_info = ""
    db.commit()

    # 第二步：一次批量请求查所有出口 IP 的纯净度
    if ips_to_check:
        purity_map = _batch_query_purity(ips_to_check)
        empty_purity = json.dumps({"level": "未知", "label": "未获取", "isp": "", "country": "", "city": ""}, ensure_ascii=False)
        for proxy in proxies:
            if proxy.exit_ip and proxy.status == 1:
                proxy.purity_info = purity_map.get(proxy.exit_ip, empty_purity)
            elif proxy.status == 0 and not proxy.purity_info:
                proxy.purity_info = ""
        db.commit()

    logger.info("代理检测完成: 可用 %d, 失效 %d", success, fail)


def get_next_proxy(db: Session):
    """
    轮询获取下一个可用代理。没有代理返回 None。
    """
    from models import Proxy

    global _round_index
    with _round_lock:
        proxies = (
            db.query(Proxy)
            .filter(Proxy.status == 1)
            .order_by(Proxy.id.asc())
            .all()
        )
        if not proxies:
            return None

        _round_index = (_round_index % len(proxies))
        selected = proxies[_round_index]
        _round_index += 1

        selected.use_count = (selected.use_count or 0) + 1
        selected.last_used_at = int(time.time())
        db.commit()
        db.refresh(selected)

        scheme = selected.proxy_type
        if scheme == "socks5" and SOCKS5_AVAILABLE:
            scheme = "socks5h"
        return selected


def get_session_proxy(db: Session, account=None) -> dict | None:
    """
    获取 requests 兼容的 proxies 配置。自动轮询。
    """
    proxy = get_next_proxy(db)
    if not proxy:
        logger.debug("无可用代理，直连请求")
        return None
    url = _build_proxy_url(proxy)
    logger.debug("使用代理: %s:%s (%s)", proxy.host, proxy.port, proxy.proxy_type)
    return {"http": url, "https": url}


def get_socks5_socket(proxy) -> tuple:
    """
    创建 SOCKS5 socket（用于 IMAP 等协议）。
    返回 (host, port) 或 None。
    需要安装 PySocks: pip install PySocks
    """
    if not SOCKS5_AVAILABLE or proxy.proxy_type != "socks5":
        return None
    return (proxy.host, proxy.port)


def create_proxied_socket(proxy, target_host: str, target_port: int, timeout: int = 30):
    """通过代理创建到目标主机的 TCP socket（不含 SSL，由调用方包装）。

    支持 SOCKS5 和 HTTP 代理（HTTP 代理通过 CONNECT 隧道）。
    需要 PySocks 库。

    返回: 已连接的 socket，或 None（代理不可用时）
    """
    if not SOCKS5_AVAILABLE:
        logger.warning("PySocks 未安装，IMAP/POP3 无法使用代理，将直连")
        return None

    import socks as socks_module

    if proxy.proxy_type == "socks5":
        proxy_type = socks_module.SOCKS5
    elif proxy.proxy_type == "http":
        proxy_type = socks_module.HTTP
    else:
        logger.warning("不支持的代理类型: %s，IMAP/POP3 将直连", proxy.proxy_type)
        return None

    sock = socks_module.socksocket()
    sock.set_proxy(
        proxy_type,
        proxy.host,
        proxy.port,
        username=proxy.username or None,
        password=proxy.password or None,
    )
    sock.settimeout(timeout)
    sock.connect((target_host, target_port))
    logger.debug(
        "通过代理 %s:%s (%s) 连接到 %s:%s",
        proxy.host, proxy.port, proxy.proxy_type, target_host, target_port,
    )
    return sock


def get_proxied_socket_factory(db: Session):
    """返回一个 socket 工厂函数，用于 imaplib/poplib 的 _create_socket 重写。

    如果没有可用代理，返回 None（调用方应直连）。

    用法:
        factory = get_proxied_socket_factory(db)
        # 在 IMAP4_SSL 子类中:
        def _create_socket(self, timeout=None):
            if factory:
                return factory(self.host, self.port, timeout)
            return super()._create_socket(timeout)
    """
    proxy = get_next_proxy(db)
    if not proxy:
        return None

    def _factory(target_host, target_port, timeout=None):
        return create_proxied_socket(proxy, target_host, target_port, timeout or 30)

    return _factory
