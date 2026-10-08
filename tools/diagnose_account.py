#!/usr/bin/env python3
"""Read-only API authentication diagnosis; never print credentials or raw replies."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from g3core import config, runtime

MAX_ERROR_BYTES = 32768
REASONS = {
    "50101": "API密钥与真钱/模拟环境不匹配。",
    "50102": "请求时间戳过期。",
    "50105": "API Passphrase不正确。",
    "50110": "欧易看到的来源IP不在此API的白名单中。",
    "50111": "API Key无效。",
    "50113": "签名验证失败；核对这组密钥是否配套。",
}


def sanitized_error(status, raw):
    result = {"http_status": int(status), "exchange_code": None,
              "reported_source_ips": []}
    try:
        if not isinstance(raw, bytes) or len(raw) > MAX_ERROR_BYTES:
            return result
        payload = json.loads(raw)
        code = payload.get("code") if isinstance(payload, dict) else None
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{1,12}", code):
            return result
        result["exchange_code"] = code
        result["meaning"] = REASONS.get(code, "请按欧易官方错误码说明核对。")
        # Never print the complete server message: it could contain sensitive data.
        # An IP is reported only for the explicit whitelist error and must parse.
        msg = payload.get("msg")
        if code == "50110" and isinstance(msg, str):
            addresses = []
            for token in re.findall(r"[0-9A-Fa-f:.]{3,}", msg):
                try:
                    ip = str(ipaddress.ip_address(token.strip(".")))
                except ValueError:
                    continue
                if ip not in addresses:
                    addresses.append(ip)
            result["reported_source_ips"] = addresses
    except (ValueError, UnicodeError):
        pass
    return result


def diagnose(cfg, creds, factory=runtime.client):
    result = {"status": "error", "orders_sent": 0}
    api = factory(cfg, creds, orders=False)
    original = api._transport

    def observe(request, **kwargs):
        # This helper only permits the two documented GET endpoints.
        if (request.get_method() != "GET" or request.full_url not in {
                cfg["origin"].rstrip("/") + "/api/v5/public/time",
                cfg["origin"].rstrip("/") + "/api/v5/account/config"}):
            raise config.Attention("诊断请求超出只读范围，已停止。")
        try:
            return original(request, **kwargs)
        except HTTPError as error:
            try:
                raw = error.read(MAX_ERROR_BYTES + 1)
            except OSError:
                raw = b""
            result.update(sanitized_error(error.code, raw))
            raise

    api._transport = observe
    try:
        api.sync_time()
        api.read("/api/v5/account/config", private=True)
        result["status"] = "authenticated"
        result["note"] = "仅账户配置读取通过；未检查交易预算、持仓核对或实际下单。"
    except Exception as error:
        result["error"] = config.safe_error(error)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出脱敏JSON")
    args = parser.parse_args(argv)
    try:
        result = diagnose(config.load_config(require_limits=False), config.credentials())
    except Exception as error:
        result = {"status": "error", "orders_sent": 0, "error": config.safe_error(error)}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        if result["status"] == "authenticated":
            print("账户身份验证通过，未下单。")
            print(result["note"])
        else:
            if "http_status" in result:
                print("HTTP状态：", result["http_status"])
                print("欧易错误码：", result["exchange_code"] or "未取得标准错误码")
            if result.get("meaning"):
                print(result["meaning"])
            for ip in result.get("reported_source_ips", []):
                print("欧易返回的来源IP：", ip)
            print(result.get("error", "诊断未完成。"))
    return 0 if result["status"] == "authenticated" else 1


if __name__ == "__main__":
    sys.exit(main())
