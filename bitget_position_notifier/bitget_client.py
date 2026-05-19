from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import requests
from requests import Response


class BitgetApiError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        response_body: str | None = None,
    ) -> None:
        self.code = code
        self.http_status = http_status
        self.response_body = response_body
        super().__init__(message)


class BitgetClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        *,
        base_url: str = "https://api.bitget.com",
        locale: str = "en-US",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.base_url = base_url.rstrip("/")
        self.locale = locale
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _timestamp_ms() -> str:
        return str(int(time.time() * 1000))

    def _sign(
        self,
        *,
        timestamp: str,
        method: str,
        request_path: str,
        query_string: str = "",
        body: str = "",
    ) -> str:
        pre_hash = f"{timestamp}{method.upper()}{request_path}"
        if query_string:
            pre_hash += f"?{query_string}"
        pre_hash += body

        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            pre_hash.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _build_headers(self, *, signature: str, timestamp: str) -> dict[str, str]:
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": signature,
            "ACCESS-PASSPHRASE": self.passphrase,
            "ACCESS-TIMESTAMP": timestamp,
            "locale": self.locale,
            "Content-Type": "application/json",
        }

    def _parse_json_response(self, response: Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise BitgetApiError(
                "Bitget response is not valid JSON",
                http_status=response.status_code,
                response_body=response.text[:1000],
            ) from exc

        if response.status_code != 200:
            raise BitgetApiError(
                f"Bitget HTTP error: {response.status_code}",
                code=str(payload.get("code")) if isinstance(payload, dict) else None,
                http_status=response.status_code,
                response_body=response.text[:1000],
            )

        if not isinstance(payload, dict):
            raise BitgetApiError(
                "Bitget response JSON is not an object",
                http_status=response.status_code,
                response_body=response.text[:1000],
            )

        if payload.get("code") != "00000":
            raise BitgetApiError(
                f"Bitget API returned error code {payload.get('code')}: {payload.get('msg')}",
                code=str(payload.get("code")),
                http_status=response.status_code,
                response_body=response.text[:1000],
            )

        return payload

    def get_all_positions(
        self,
        *,
        product_type: str = "USDT-FUTURES",
        margin_coin: str = "USDT",
    ) -> list[dict[str, Any]]:
        request_path = "/api/v2/mix/position/all-position"
        sorted_params = sorted(
            [("productType", product_type), ("marginCoin", margin_coin)],
            key=lambda item: item[0],
        )
        query_string = urlencode(sorted_params)
        timestamp = self._timestamp_ms()
        signature = self._sign(
            timestamp=timestamp,
            method="GET",
            request_path=request_path,
            query_string=query_string,
        )
        headers = self._build_headers(signature=signature, timestamp=timestamp)

        try:
            response = requests.get(
                f"{self.base_url}{request_path}",
                params=sorted_params,
                headers=headers,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise BitgetApiError(f"Bitget request failed: {exc}") from exc

        payload = self._parse_json_response(response)
        data = payload.get("data", [])
        if not isinstance(data, list):
            raise BitgetApiError(
                "Bitget API data field is not a list",
                code=str(payload.get("code")),
                http_status=response.status_code,
                response_body=response.text[:1000],
            )
        return data
