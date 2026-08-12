"""Model API client (OpenAI-compatible) for user test01.

Talks to the GLM-5.2 endpoint at MODEL_BASE_URL. The same long prefix is sent
on every call so that request #1 fills the prompt cache and the remaining
requests hit it.
"""

import httpx


class ModelClient:
    def __init__(
        self, base_url, api_key, chat_path="/v1/chat/completions", timeout=120.0
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.chat_path = chat_path
        self.client = httpx.Client(timeout=timeout)
        self.client.headers["Authorization"] = f"Bearer {api_key}"

    def chat(self, messages, model="glm-5.2", max_tokens=64, temperature=0.0):
        url = f"{self.base_url}{self.chat_path}"
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        resp = self.client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()

    def close(self):
        self.client.close()
