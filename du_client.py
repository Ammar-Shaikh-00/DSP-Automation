from __future__ import annotations

import json
import logging
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BASE_URL = "https://dusales.ae"
VFE_URL = f"{BASE_URL}/services/vfe"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0"
)

SESSION_EXPIRED_MARKERS = (
    "session expired",
    "expiredsession",
    "please login again",
    "session error",
)

HTTP_STATUS_MARKER = "\n__DU_HTTP_STATUS__:"
CURL_CONNECT_TIMEOUT = "10"
CURL_MAX_TIME = "45"


class DUError(Exception):
    """Base DU automation error. Messages must never include secrets."""


class DUAuthenticationError(DUError):
    """Login or session recovery failed."""


class DUAccountNotFoundError(DUError):
    """The Account ID was not found in DU."""


class DUNoServicesError(DUError):
    """The account has no Fiber or Wireless services."""


class DUAPIError(DUError):
    """A DU API call failed after any allowed retry."""


class DUInvalidInputError(DUError):
    """Column C is neither an Account ID nor a FIX ID."""


def now_ms() -> str:
    return str(int(time.time() * 1000))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _format_location(geographic_address: Any, building_name: Any) -> str:
    parts = [part for part in (_text(geographic_address), _text(building_name)) if part]
    return " | ".join(parts)


def classify_input_id(input_id: str) -> str:
    value = str(input_id).strip()
    if value.startswith("1."):
        return "ACCOUNT_ID"
    if value.isdigit():
        return "FIX_ID"
    return "INVALID"


class DUClient:
    def __init__(
        self,
        username: str,
        password: str,
        cookie_file: str | Path = "du_cookies.txt",
        work_dir: str | Path | None = None,
        debug: bool = False,
    ) -> None:
        self.username = username
        self.password = password
        self.debug = debug
        self.token: str | None = None
        self.profile: str | None = None
        self.profile_id: str | None = None
        self.api_time = 0.0
        self.last_login_time = 0.0
        self.last_qbe_time = 0.0
        self.last_home_time = 0.0
        self.last_wireless_time = 0.0
        self.cache_hits = 0
        self.api_calls_avoided = 0
        self.account_cache: dict[str, dict[str, Any]] = {}
        self.home_services_cache: dict[str, tuple[list[Any], dict[str, Any]]] = {}
        self.wireless_services_cache: dict[str, list[Any]] = {}

        base = Path(work_dir) if work_dir is not None else Path.cwd()
        self.cookie_file = Path(cookie_file)
        if not self.cookie_file.is_absolute():
            self.cookie_file = base / self.cookie_file

        self.header_file = base / "temp_headers.txt"
        self.session_file = base / "du_session.json"
        self.login_body_file = base / "login_response.json"
        self.login_header_file = base / "login_headers.txt"

    def _run_curl(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise DUAPIError(
                "curl.exe was not found. It is required for DU HTTP calls."
            ) from exc

    def curl_json(
        self,
        payload: dict[str, Any],
        headers: list[str],
    ) -> tuple[str, dict[str, Any] | None, str, str]:
        self.header_file.unlink(missing_ok=True)

        cmd = [
            "curl.exe",
            "--silent",
            "--show-error",
            "--location",
            "--connect-timeout", CURL_CONNECT_TIMEOUT,
            "--max-time", CURL_MAX_TIME,
            "--cookie", str(self.cookie_file),
            "--cookie-jar", str(self.cookie_file),
            "--url", VFE_URL,
            "--dump-header", str(self.header_file),
            "--write-out", HTTP_STATUS_MARKER + "%{http_code}",
        ]

        for header in headers:
            cmd.extend(["-H", header])

        cmd.extend([
            "--data-raw",
            json.dumps(payload),
        ])

        started = time.perf_counter()
        try:
            result = self._run_curl(cmd)
        finally:
            self.api_time += time.perf_counter() - started

        if result.returncode != 0:
            raise DUAPIError("DU HTTP request failed.")

        stdout = result.stdout or ""
        marker_index = stdout.rfind(HTTP_STATUS_MARKER)
        if marker_index < 0:
            raise DUAPIError("DU HTTP request failed.")

        body = stdout[:marker_index]
        status = stdout[marker_index + len(HTTP_STATUS_MARKER):].strip()

        response_headers = ""
        if self.header_file.exists():
            response_headers = self.header_file.read_text(
                encoding="utf-8",
                errors="replace",
            )

        try:
            data = json.loads(body) if body else None
        except Exception:
            data = None

        if data is not None and not isinstance(data, dict):
            data = None

        return status, data, body, response_headers

    def update_token_from_headers(self, headers_text: str) -> None:
        new_token = None

        for line in headers_text.splitlines():
            if line.lower().startswith("xcsrftoken:"):
                value = line.split(":", 1)[1].strip()
                if value and value.lower() != "undefined":
                    new_token = value

        if new_token:
            self.token = new_token

    def _load_session(self) -> dict[str, str] | None:
        if not self.session_file.exists() or not self.cookie_file.exists():
            return None
        try:
            raw = json.loads(self.session_file.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        username = _text(raw.get("username"))
        token = _text(raw.get("token"))
        profile = _text(raw.get("profile"))
        profile_id = _text(raw.get("profile_id"))
        if username.lower() != self.username.lower():
            return None
        if not token or not profile or not profile_id:
            return None
        return {
            "token": token,
            "profile": profile,
            "profile_id": profile_id,
        }

    def _save_session(self) -> None:
        if not self.token or not self.profile or self.profile_id is None:
            return
        payload = {
            "username": self.username,
            "token": self.token,
            "profile": self.profile,
            "profile_id": self.profile_id,
        }
        self.session_file.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )

    def _clear_session_state(self) -> None:
        self.token = None
        self.profile = None
        self.profile_id = None

    def _try_reuse_session(self) -> bool:
        state = self._load_session()
        if not state:
            return False

        self.token = state["token"]
        self.profile = state["profile"]
        self.profile_id = state["profile_id"]

        try:
            self.initialize_profile(log_success=False)
        except (DUAPIError, DUAuthenticationError):
            self._clear_session_state()
            return False

        self._save_session()
        return True

    def ensure_session(self) -> None:
        started = time.perf_counter()
        try:
            if self._try_reuse_session():
                logger.info("Existing DU session is valid.")
                return
            logger.info("Existing DU session missing or invalid.")
            self.login()
        finally:
            self.last_login_time = time.perf_counter() - started

    def bootstrap(self) -> None:
        if self.cookie_file.exists():
            self.cookie_file.unlink()

        cmd = [
            "curl.exe",
            "--silent",
            "--show-error",
            "--location",
            "--connect-timeout", CURL_CONNECT_TIMEOUT,
            "--max-time", CURL_MAX_TIME,
            "--cookie", str(self.cookie_file),
            "--cookie-jar", str(self.cookie_file),
            "-H", f"User-Agent: {USER_AGENT}",
            f"{BASE_URL}/index.html",
        ]

        started = time.perf_counter()
        try:
            result = self._run_curl(cmd)
        except DUAPIError as exc:
            raise DUAuthenticationError(str(exc)) from exc
        finally:
            self.api_time += time.perf_counter() - started

        if result.returncode != 0:
            raise DUAuthenticationError("DU bootstrap failed.")

    def login(self) -> None:
        logger.info("Logging into DU...")
        started = time.perf_counter()

        try:
            self.bootstrap()

            payload = {
                "loginWithoutProfileSelection": {
                    "username": self.username.lower(),
                    "password": self.password,
                    "hostname": "",
                    "ipAddresses": "",
                    "macAddresses": "",
                    "otp": None,
                    "cardStatusXML": "",
                    "biometricXML": "",
                    "geoCoordinates": None,
                    "imei": None,
                    "deviceType": "Tablet",
                    "deviceId": "",
                }
            }

            headers = [
                "Accept: */*",
                "Cache-Control: no-cache",
                "Pragma: no-cache",
                "Client-Context: null",
                "Client-Hostname: null",
                "Client-IP: null",
                "Client-Location: null",
                "Client-Process: Login",
                "Client-Profile: null",
                "Client-Session: null",
                f"Client-Time: {now_ms()}",
                "Client-Username: null",
                "Content-Type: application/json; charset=UTF-8",
                "Origin: https://dusales.ae",
                "Referer: https://dusales.ae/index.html",
                f"User-Agent: {USER_AGENT}",
                "X-Requested-With: XMLHttpRequest",
                "xCsrfToken: undefined",
            ]

            status, data, body, response_headers = self.curl_json(
                payload,
                headers,
            )

            if self.debug:
                self.login_body_file.write_text(body, encoding="utf-8")
                self.login_header_file.write_text(response_headers, encoding="utf-8")

            if status != "200":
                raise DUAuthenticationError(f"Login HTTP {status}")

            if not data:
                raise DUAuthenticationError("Login returned invalid JSON.")

            ret = _as_dict(data.get("return"))

            if ret.get("statusCode") != "OK":
                raise DUAuthenticationError(
                    f"Login failed: statusCode={ret.get('statusCode')}"
                )

            access_token = ret.get("accessToken")
            profile = ret.get("profileName")
            profile_id = ret.get("profileId")

            if not access_token:
                raise DUAuthenticationError(
                    "Login succeeded but accessToken missing."
                )

            if not profile or profile_id is None:
                raise DUAuthenticationError(
                    "Login succeeded but profile details were missing."
                )

            self.token = str(access_token)
            self.profile = str(profile)
            self.profile_id = str(profile_id)

            logger.info("DU login successful.")

            try:
                self.initialize_profile()
            except DUAPIError as exc:
                raise DUAuthenticationError(
                    "DU login succeeded but profile initialization failed."
                ) from exc
            self._save_session()
        finally:
            self.last_login_time = time.perf_counter() - started

    def auth_headers(self) -> list[str]:
        return [
            "Accept: */*",
            "Cache-Control: no-cache",
            "Pragma: no-cache",
            "Client-Context:",
            "Client-Hostname:",
            "Client-IP: null",
            "Client-Location: null",
            "Client-Process: Context",
            f"Client-Profile: {self.profile}",
            "Client-Session: null",
            f"Client-Time: {now_ms()}",
            f"Client-Username: {self.username}",
            "Content-Type: application/json; charset=UTF-8",
            "Origin: https://dusales.ae",
            "Referer: https://dusales.ae/home.html",
            f"User-Agent: {USER_AGENT}",
            "X-Requested-With: XMLHttpRequest",
            f"xCsrfToken: {self.token}",
        ]

    def _session_failed(self, status: str, data: dict[str, Any] | None, body: str) -> bool:
        if status in ("401", "403"):
            return True

        haystack = ""
        if data is not None:
            haystack += str(data.get("return", data)).lower()
        if body:
            haystack += body.lower()

        return any(marker in haystack for marker in SESSION_EXPIRED_MARKERS)

    def api(self, payload: dict[str, Any], retry: bool = True) -> dict[str, Any]:
        status, data, body, response_headers = self.curl_json(
            payload,
            self.auth_headers(),
        )

        self.update_token_from_headers(response_headers)

        if self._session_failed(status, data, body) and retry:
            logger.info(
                "Session expired/rejected. Logging in again automatically..."
            )
            self.login()
            return self.api(payload, retry=False)

        if status != "200":
            raise DUAPIError(f"DU API HTTP {status}")

        if not data:
            raise DUAPIError("DU returned invalid JSON.")

        return data

    def initialize_profile(self, log_success: bool = True) -> None:
        payload = {
            "getProfileProcesses": {
                "profileId": self.profile_id,
                "username": self.username,
                "emiratesIdExpired": "false",
            }
        }

        self.api(payload, retry=False)
        if log_success:
            logger.info("Profile initialized.")

    def _cache_account(self, resolved_account_id: str, common: dict[str, Any]) -> None:
        key = _text(resolved_account_id)
        if not key:
            return
        self.account_cache[key] = {
            "account_name": common.get("account_name"),
            "phone_number": common.get("phone_number"),
            "nationality": common.get("nationality"),
            "siebel_account_id": common.get("siebel_account_id"),
        }

    def _cached_account(self, resolved_account_id: str) -> dict[str, Any] | None:
        key = _text(resolved_account_id)
        if not key:
            return None
        cached = self.account_cache.get(key)
        if cached is None:
            return None
        self.cache_hits += 1
        self.api_calls_avoided += 1
        return deepcopy(cached)

    def get_qbe(self, account_id: str) -> dict[str, Any]:
        cached = self._cached_account(account_id)
        if cached is not None:
            self.last_qbe_time = 0.0
            return {
                "account_id": account_id,
                "input_id": account_id,
                "input_type": "ACCOUNT_ID",
                "resolved_account_id": "",
                "account_name": cached.get("account_name"),
                "phone_number": cached.get("phone_number"),
                "nationality": cached.get("nationality"),
                "siebel_account_id": cached.get("siebel_account_id"),
                "msisdn": "",
            }

        payload = {
            "getQBEInfo": {
                "accountCode": account_id,
                "dateOfBirth": None,
                "phoneNumber": None,
                "docNumber": None,
                "nationality": None,
                "securityToken": None,
                "username": self.username,
                "findIsSameClient": True,
            }
        }

        started = time.perf_counter()
        try:
            data = self.api(payload)
        finally:
            self.last_qbe_time = time.perf_counter() - started

        ret = _as_dict(data.get("return"))
        account = _as_dict(ret.get("account"))
        customer = _as_dict(ret.get("customer"))

        if not account.get("accountCode") and not account.get("id"):
            raise DUAccountNotFoundError("Account not found")

        common = {
            "account_id": account_id,
            "input_id": account_id,
            "input_type": "ACCOUNT_ID",
            "resolved_account_id": "",
            "account_name": account.get("accountName"),
            "phone_number": account.get("phoneNumber"),
            "nationality": (
                customer.get("nationality")
                or account.get("nationality")
            ),
            "siebel_account_id": account.get("id"),
            "msisdn": "",
        }
        self._cache_account(account_id, common)
        return common

    def get_qbe_by_fix_id(self, fix_id: str) -> dict[str, Any]:
        payload = {
            "getQBEInfo": {
                "msisdn": fix_id,
                "dateOfBirth": None,
                "phoneNumber": None,
                "docNumber": None,
                "nationality": None,
                "securityToken": None,
                "username": self.username,
                "findIsSameClient": True,
                "forceCustomer": True,
                "sendCode": False,
                "code": None,
            }
        }

        started = time.perf_counter()
        try:
            data = self.api(payload)
        finally:
            self.last_qbe_time = time.perf_counter() - started

        ret = _as_dict(data.get("return"))
        account = _as_dict(ret.get("account"))
        customer = _as_dict(ret.get("customer"))
        mobile = _as_dict(ret.get("mobile"))

        resolved_account_id = _text(account.get("accountCode"))
        if not resolved_account_id and not account.get("id"):
            raise DUAccountNotFoundError("Account not found")

        common = {
            "account_id": resolved_account_id,
            "input_id": fix_id,
            "input_type": "FIX_ID",
            "resolved_account_id": resolved_account_id,
            "account_name": account.get("accountName"),
            "phone_number": account.get("phoneNumber"),
            "nationality": (
                customer.get("nationality")
                or account.get("nationality")
            ),
            "siebel_account_id": account.get("id"),
            "msisdn": _text(mobile.get("msisdn")) or fix_id,
        }
        self._cache_account(resolved_account_id, common)
        return common

    def get_home_services(
        self,
        account_id: str,
        siebel_account_id: Any,
    ) -> tuple[list[Any], dict[str, Any]]:
        cache_key = _text(account_id)
        cached = self.home_services_cache.get(cache_key)
        if cache_key and cached is not None:
            self.cache_hits += 1
            self.api_calls_avoided += 1
            self.last_home_time = 0.0
            return deepcopy(cached)

        payload = {
            "getHomeServicesAssetsResponse": {
                "accountCode": account_id,
                "siebelAccountId": siebel_account_id,
                "profile": self.profile,
            }
        }

        started = time.perf_counter()
        try:
            data = self.api(payload)
        finally:
            self.last_home_time = time.perf_counter() - started

        ret = _as_dict(data.get("return"))
        services = _as_list(ret.get("homeServicesAssets"))
        address = _as_dict(ret.get("homeAddressDetails"))
        result = (services, address)
        if cache_key:
            self.home_services_cache[cache_key] = deepcopy(result)
        return result

    def get_wireless_services(self, account_id: str) -> list[Any]:
        cache_key = _text(account_id)
        cached = self.wireless_services_cache.get(cache_key)
        if cache_key and cached is not None:
            self.cache_hits += 1
            self.api_calls_avoided += 1
            self.last_wireless_time = 0.0
            return deepcopy(cached)

        payload = {
            "getAccountServices": {
                "accountCode": account_id,
            }
        }

        started = time.perf_counter()
        try:
            data = self.api(payload)
        finally:
            self.last_wireless_time = time.perf_counter() - started

        assets = _as_list(_as_dict(data.get("return")).get("assets"))
        if cache_key:
            self.wireless_services_cache[cache_key] = deepcopy(assets)
        return assets

    def process_account(self, input_id: str) -> dict[str, Any]:
        self.last_qbe_time = 0.0
        self.last_home_time = 0.0
        self.last_wireless_time = 0.0

        input_id = str(input_id).strip()
        input_type = classify_input_id(input_id)
        if input_type == "INVALID":
            raise DUInvalidInputError("Invalid input")

        if input_type == "FIX_ID":
            common = self.get_qbe_by_fix_id(input_id)
            account_id = _text(common.get("resolved_account_id"))
            if not account_id:
                raise DUAccountNotFoundError("Account not found")
            resolved_account_id = account_id
            fiber_contact = _text(common.get("msisdn")) or input_id
        else:
            common = self.get_qbe(input_id)
            account_id = input_id
            resolved_account_id = ""
            fiber_contact = common["phone_number"]

        siebel_account_id = common.get("siebel_account_id")
        if siebel_account_id is None or _text(siebel_account_id) == "":
            raise DUAPIError("Missing required values")

        services, address = self.get_home_services(
            account_id,
            siebel_account_id,
        )

        if services:
            logger.info("Detected: FIBER")
            service = _as_dict(services[0])
            row = {
                "activation_date": service.get("activationDate"),
                "account_name": common["account_name"],
                "account_id": input_id,
                "input_id": input_id,
                "resolved_account_id": resolved_account_id,
                "contact": fiber_contact,
                "phone_nr": common["phone_number"],
                "nationality": common["nationality"],
                "promotion": service.get("promotionName"),
                "location": _format_location(
                    address.get("geographicAddressId"),
                    address.get("buildingName"),
                ),
                "account_type": "FIBER",
                "status": "SUCCESS",
            }
            return {
                "account_id": input_id,
                "input_id": input_id,
                "resolved_account_id": resolved_account_id,
                "type": "FIBER",
                "rows": [row],
            }

        logger.info("Detected: WIRELESS")
        assets = self.get_wireless_services(account_id)

        if not assets:
            raise DUNoServicesError("No services found")

        logger.info("%s wireless service%s found.", len(assets), "" if len(assets) == 1 else "s")

        rows = []
        for asset in assets:
            item = _as_dict(asset)
            rows.append({
                "activation_date": item.get("activationDate"),
                "account_name": common["account_name"],
                "account_id": input_id,
                "input_id": input_id,
                "resolved_account_id": resolved_account_id,
                "contact": item.get("serviceId"),
                "phone_nr": common["phone_number"],
                "nationality": common["nationality"],
                "promotion": item.get("ratePlanName"),
                "location": "",
                "account_type": "WIRELESS",
                "status": "SUCCESS",
                "contract_code": item.get("contractCode"),
                "asset_status": item.get("status"),
            })

        return {
            "account_id": input_id,
            "input_id": input_id,
            "resolved_account_id": resolved_account_id,
            "type": "WIRELESS",
            "rows": rows,
        }
