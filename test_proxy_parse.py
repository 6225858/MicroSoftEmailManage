import sys
sys.path.insert(0, ".")
from proxy_service import import_proxy_line

tests = [
    "http://user:pass@10.0.0.1:3128",
    "socks5://admin:123456@192.168.1.1:1080",
    "proxy_user:my_pwd@10.0.0.2:8080",
    "10.0.0.3:8080",
]

for t in tests:
    r = import_proxy_line(t)
    if r:
        print(f"input : {t}")
        print(f"  type: {r['proxy_type']}, host: {r['host']}:{r['port']}")
        print(f"  auth: {r['username']}:{r['password']}")
        print()
    else:
        print(f"input : {t} -> FAILED\n")
