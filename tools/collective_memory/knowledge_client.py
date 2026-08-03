from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests


class KnowledgeClientError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None,
                 url: str | None = None, response_text: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.response_text = response_text


@dataclass(frozen=True)
class KnowledgeQuery:
    project: str
    keywords: list[str]
    modules: list[str]
    limit: int = 5


class KnowledgeClient:
    def create_entry(self, collection: str, entry: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def update_entry(self, collection: str, entry_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def search_entries(self, collection: str, query: KnowledgeQuery) -> list[dict[str, Any]]:
        raise NotImplementedError


class HubKnowledgeClient(KnowledgeClient):
    """Hub Knowledge API 适配（/api/v1/knowledge）。"""

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 api_prefix: str = '/api/v1', timeout: int = 30):
        self.base_url = (base_url or os.environ.get('HUB_API_BASE', '')).rstrip('/')
        self.token = token or os.environ.get('HUB_API_TOKEN', '')
        self.api_prefix = api_prefix.rstrip('/')
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        return headers

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f'{self.base_url}{self.api_prefix}{path}'
        try:
            resp = requests.request(
                method, url, headers=self._headers(), timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise KnowledgeClientError(
                f'Hub Knowledge 请求失败: {exc}', url=url) from exc

        if not resp.ok:
            raise KnowledgeClientError(
                f'Hub Knowledge 返回 {resp.status_code}',
                status_code=resp.status_code,
                url=url,
                response_text=resp.text,
            )
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def create_entry(self, collection: str, entry: dict[str, Any]) -> dict[str, Any]:
        if collection != 'pitfall':
            entry = dict(entry)
        return self._request('POST', '/knowledge', json=entry)

    def update_entry(self, collection: str, entry_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        return self._request('PUT', f'/knowledge/{entry_id}', json=patch)

    def search_entries(self, collection: str, query: KnowledgeQuery) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            'category': 'pitfall',
            'project': query.project,
            'status': 'approved',
        }
        if query.modules:
            params['module'] = query.modules[0]

        search_term = ' '.join(query.keywords).strip()
        if search_term:
            params['search'] = search_term

        entries = self._request('GET', '/knowledge', params=params)
        if not isinstance(entries, list):
            return []
        return entries[:query.limit]
