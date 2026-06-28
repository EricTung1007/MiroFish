"""
LLM客戶端封裝
統一使用OpenAI格式調用
"""

import json
import re
from typing import Optional, Dict, Any, List
from openai import BadRequestError
from openai import OpenAI

from ..config import Config


class LLMClient:
    """LLM客戶端"""
    
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
            raise ValueError("LLM_API_KEY 未配置")
        
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None
    ) -> str:
        """
        發送聊天請求
        
        Args:
            messages: 消息列表
            temperature: 溫度參數
            max_tokens: 最大token數
            response_format: 響應格式（如JSON模式）
            
        Returns:
            模型響應文本
        """
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        
        if response_format:
            kwargs["response_format"] = response_format
        
        response = self.client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content
        # 部分模型（如MiniMax M2.5）會在content中包含<think>思考內容，需要移除
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        return content
    
    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        json_schema: Optional[Dict[str, Any]] = None,
        schema_name: str = "json_response"
    ) -> Dict[str, Any]:
        """
        發送聊天請求並返回JSON
        
        Args:
            messages: 消息列表
            temperature: 溫度參數
            max_tokens: 最大token數
            
        Returns:
            解析後的JSON對象
        """
        try:
            response = self.chat(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"}
            )
        except BadRequestError as exc:
            message = str(exc)
            if "response_format" not in message:
                raise
            try:
                response = self.chat(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=self._json_schema_format(json_schema, schema_name)
                )
            except BadRequestError:
                response = self.chat(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens
                )

        # 清理markdown代碼塊標記
        cleaned_response = response.strip()
        cleaned_response = re.sub(r'^```(?:json)?\s*\n?', '', cleaned_response, flags=re.IGNORECASE)
        cleaned_response = re.sub(r'\n?```\s*$', '', cleaned_response)
        cleaned_response = cleaned_response.strip()

        try:
            return json.loads(cleaned_response)
        except json.JSONDecodeError:
            json_match = re.search(r'\{[\s\S]*\}', cleaned_response)
            if json_match:
                try:
                    return json.loads(json_match.group(0))
                except json.JSONDecodeError:
                    pass
            raise ValueError(f"LLM返回的JSON格式無效: {cleaned_response}")

    @staticmethod
    def _json_schema_format(
        json_schema: Optional[Dict[str, Any]],
        schema_name: str
    ) -> Dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": False,
                "schema": json_schema or {
                    "type": "object",
                    "additionalProperties": True
                }
            }
        }
