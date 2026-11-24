"""Preserve the official DeepSeek reasoning field across tool-call turns."""

from copy import deepcopy

from langchain_openai import ChatOpenAI


class DeepSeekChatOpenAI(ChatOpenAI):
    wire_native_thinking_only: bool = False

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        if any((c.get("delta") or {}).get("role") == "" for c in chunk.get("choices", [])):
            chunk = deepcopy(chunk)
            for choice in chunk.get("choices", []):
                if (choice.get("delta") or {}).get("role") == "":
                    choice["delta"]["role"] = "assistant"
        result = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        if result is not None and chunk.get("choices"):
            reasoning = (chunk["choices"][0].get("delta") or {}).get("reasoning_content")
            if reasoning is not None:
                result.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _create_chat_result(self, response, generation_info=None):
        data = response if isinstance(response, dict) else response.model_dump()
        # Some compatible gateways omit the assistant role while returning
        # valid completion/tool fields. Normalize before LangChain conversion
        # so tool calls are parsed and validated rather than silently lost.
        if any(not (c.get("message", {}).get("role") or "").strip()
               for c in data.get("choices", [])):
            data = deepcopy(data)
            for choice in data.get("choices", []):
                if not (choice.get("message", {}).get("role") or "").strip():
                    choice["message"]["role"] = "assistant"
            response = data
        result = super()._create_chat_result(response, generation_info)
        for generation, choice in zip(result.generations, data.get("choices", [])):
            reasoning = choice.get("message", {}).get("reasoning_content")
            if reasoning is not None:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if self.wire_native_thinking_only:
            if self.reasoning_effort != "none" or (self.extra_body or {}).get("thinking",{}).get("type") != "disabled":
                raise ValueError("Native disabled compatibility requires configured none and thinking=disabled")
            payload.pop("reasoning_effort", None)
        if (self.extra_body or {}).get("thinking", {}).get("type") == "disabled":
            return payload
        for encoded, message in zip(
            payload["messages"], self._convert_input(input_).to_messages()
        ):
            if (
                message.type == "ai"
                and "reasoning_content" in message.additional_kwargs
            ):
                encoded["reasoning_content"] = message.additional_kwargs[
                    "reasoning_content"
                ]
        return payload
