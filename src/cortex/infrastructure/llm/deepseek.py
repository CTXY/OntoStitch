"""Preserve the official DeepSeek reasoning field across tool-call turns."""

from langchain_openai import ChatOpenAI


class DeepSeekChatOpenAI(ChatOpenAI):
    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        data = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, data.get("choices", [])):
            reasoning = choice.get("message", {}).get("reasoning_content")
            if reasoning is not None:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
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
