"""
LLM Client Wrapper
Unified calls using OpenAI format
"""

import json
import logging
import re
import time
from typing import Optional, Dict, Any, List
from openai import OpenAI, APIStatusError, APIConnectionError, APITimeoutError

from ..config import Config

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


class LLMClient:
    """LLM Client"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None
    ):
        self.api_key = api_key or Config.LLM_API_KEY
        self.base_url = base_url or Config.LLM_BASE_URL
        self.model = model or Config.LLM_MODEL_NAME

        if not self.api_key:
            raise ValueError("LLM_API_KEY not configured")

        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )

    @classmethod
    def create_boost(cls) -> 'LLMClient':
        """Return a boost LLM client (large context) if configured, else the default client."""
        if Config.LLM_BOOST_MODEL_NAME and Config.LLM_BOOST_API_KEY:
            return cls(
                api_key=Config.LLM_BOOST_API_KEY,
                base_url=Config.LLM_BOOST_BASE_URL,
                model=Config.LLM_BOOST_MODEL_NAME,
            )
        logger.warning("LLM_BOOST not configured — falling back to default LLM for section generation")
        return cls()
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None
    ) -> str:
        """
        Send chat request
        
        Args:
            messages: Message list
            temperature: Temperature parameter
            max_tokens: Maximum number of tokens
            response_format: Response format (e.g., JSON mode)
            
        Returns:
            Model response text
        """
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        
        if response_format:
            kwargs["response_format"] = response_format

        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(**kwargs)
                break
            except (APIConnectionError, APITimeoutError) as e:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"LLM connection error (attempt {attempt+1}/{max_retries}), retrying in {wait}s: {e}")
                    time.sleep(wait)
                else:
                    raise
            except APIStatusError as e:
                if e.status_code in _RETRYABLE_STATUS_CODES and attempt < max_retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"LLM API {e.status_code} error (attempt {attempt+1}/{max_retries}), retrying in {wait}s: {e}")
                    time.sleep(wait)
                else:
                    raise

        message = response.choices[0].message
        content = message.content or ''
        # Some models put reasoning in <think> tags inside content; strip them
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        # Extended thinking models (e.g. Ollama cloud) may put the answer in a
        # separate `reasoning` field when content is empty — fall back to it
        if not content:
            reasoning = getattr(message, 'reasoning', None) or getattr(message, 'reasoning_content', None)
            if reasoning:
                content = str(reasoning).strip()
        return content
    
    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """
        Send chat request and return JSON

        Args:
            messages: Message list
            temperature: Temperature parameter
            max_tokens: Maximum number of tokens

        Returns:
            Parsed JSON object
        """
        # Try with json_object response format first; fall back to plain text if unsupported
        try:
            response = self.chat(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"}
            )
        except Exception as e:
            logger.warning(f"json_object response_format not supported, retrying without it: {e}")
            response = self.chat(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        return self._parse_json_response(response)

    def _parse_json_response(self, response: str) -> Dict[str, Any]:
        """Extract and parse JSON from an LLM response string."""
        cleaned = response.strip()
        # Strip markdown code fences
        cleaned = re.sub(r'^```(?:json)?\s*\n?', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\n?```\s*$', '', cleaned)
        cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Try extracting the first {...} block as a last resort
            match = re.search(r'\{[\s\S]*\}', cleaned)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
            raise ValueError(f"Invalid JSON format returned by LLM: {cleaned[:500]}")

