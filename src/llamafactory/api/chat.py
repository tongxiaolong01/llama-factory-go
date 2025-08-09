# Copyright 2025 the LlamaFactory team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import io
import ipaddress
import json
import os
import re
import socket
import uuid
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

from ..data import Role as DataRole
from ..extras import logging
from ..extras.constants import AUDIO_PLACEHOLDER, IMAGE_PLACEHOLDER, VIDEO_PLACEHOLDER
from ..extras.misc import is_env_enabled
from ..extras.packages import is_fastapi_available, is_pillow_available, is_requests_available
from .common import dictify, jsonify
from .protocol import (
    ChatCompletionMessage,
    ChatCompletionResponse,
    ChatCompletionResponseChoice,
    ChatCompletionResponseUsage,
    ChatCompletionStreamResponse,
    ChatCompletionStreamResponseChoice,
    Finish,
    Function,
    FunctionCall,
    Role,
    ScoreEvaluationResponse,
)

# 从环境变量读取安全媒体路径，提供默认值
SAFE_MEDIA_PATH = os.environ.get("SAFE_MEDIA_PATH", os.path.join(os.path.dirname(__file__), "safe_media"))
# 允许通过环境变量禁用本地文件加载功能
ALLOW_LOCAL_FILES = is_env_enabled("ALLOW_LOCAL_FILES", "true")
# 新增：允许的URL前缀白名单，以逗号分隔
# 例如: "https://s3.amazonaws.com/,https://storage.googleapis.com/"
ALLOWED_URL_PREFIXES_STR = os.environ.get("ALLOWED_URL_PREFIXES", "")
ALLOWED_URL_PREFIXES = [
    prefix.strip() for prefix in ALLOWED_URL_PREFIXES_STR.split(",") if prefix.strip()
]
# 确保安全目录存在
if not os.path.exists(SAFE_MEDIA_PATH):
    os.makedirs(SAFE_MEDIA_PATH)


def _check_lfi_path(path: str) -> None:
    """
    Checks if a given path is vulnerable to LFI. Raises HTTPException if unsafe.
    """
    if not ALLOW_LOCAL_FILES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Local file access is disabled.")

    # 规范化路径并检查它是否在安全目录下
    try:
        # 关键防护：os.path.realpath会解析".."和符号链接，得到真实的物理路径
        real_path = os.path.realpath(path)
        safe_path = os.path.realpath(SAFE_MEDIA_PATH)

        # 关键防护：如果解析后的真实路径不是以安全目录为前缀，则拒绝访问
        if not real_path.startswith(safe_path):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="File access is restricted to the safe media directory.")
    except Exception:  # 捕获路径不存在等文件系统错误
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or inaccessible file path.")


def _check_ssrf_url(url: str) -> None:
    """
    Checks if a given URL is vulnerable to SSRF, with an optional whitelist.
    Raises HTTPException if unsafe.
    """
    # 1. 新增：URL前缀白名单检查
    if ALLOWED_URL_PREFIXES:
        # 如果设置了白名单，则URL必须以其中一个前缀开头
        if not any(url.startswith(prefix) for prefix in ALLOWED_URL_PREFIXES):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="URL is not in the allowed list of prefixes.",
            )

    # 2. 现有的IP地址检查 (即使通过白名单也必须执行，以防DNS重绑定)
    try:
        parsed_url = urlparse(url)
        if parsed_url.scheme not in ["http", "https"]:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only HTTP/HTTPS URLs are allowed.")

        hostname = parsed_url.hostname
        if not hostname:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid URL hostname.")

        ip_info = socket.getaddrinfo(hostname, parsed_url.port)
        ip_address_str = ip_info[0][4][0]
        ip = ipaddress.ip_address(ip_address_str)

        if not ip.is_global:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"Access to private or reserved IP addresses is not allowed.")

    except socket.gaierror:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Could not resolve hostname: {parsed_url.hostname}")
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid URL: {e}")

if is_fastapi_available():
    from fastapi import HTTPException, status


if is_pillow_available():
    from PIL import Image


if is_requests_available():
    import requests


if TYPE_CHECKING:
    from ..chat import ChatModel
    from ..data.mm_plugin import AudioInput, ImageInput, VideoInput
    from .protocol import ChatCompletionRequest, ScoreEvaluationRequest


logger = logging.get_logger(__name__)
ROLE_MAPPING = {
    Role.USER: DataRole.USER.value,
    Role.ASSISTANT: DataRole.ASSISTANT.value,
    Role.SYSTEM: DataRole.SYSTEM.value,
    Role.FUNCTION: DataRole.FUNCTION.value,
    Role.TOOL: DataRole.OBSERVATION.value,
}


def _process_request(
    request: "ChatCompletionRequest",
) -> tuple[
    list[dict[str, str]],
    Optional[str],
    Optional[str],
    Optional[list["ImageInput"]],
    Optional[list["VideoInput"]],
    Optional[list["AudioInput"]],
]:
    if is_env_enabled("API_VERBOSE", "1"):
        logger.info_rank0(f"==== request ====\n{json.dumps(dictify(request), indent=2, ensure_ascii=False)}")

    if len(request.messages) == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid length")

    if request.messages[0].role == Role.SYSTEM:
        content = request.messages.pop(0).content
        system = content[0].text if isinstance(content, list) else content
    else:
        system = None

    if len(request.messages) % 2 == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only supports u/a/u/a/u...")

    input_messages = []
    images, videos, audios = [], [], []
    for i, message in enumerate(request.messages):
        if i % 2 == 0 and message.role not in [Role.USER, Role.TOOL]:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")
        elif i % 2 == 1 and message.role not in [Role.ASSISTANT, Role.FUNCTION]:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")

        if message.role == Role.ASSISTANT and isinstance(message.tool_calls, list) and len(message.tool_calls):
            tool_calls = [
                {"name": tool_call.function.name, "arguments": tool_call.function.arguments}
                for tool_call in message.tool_calls
            ]
            content = json.dumps(tool_calls, ensure_ascii=False)
            input_messages.append({"role": ROLE_MAPPING[Role.FUNCTION], "content": content})
        elif isinstance(message.content, list):
            text_content = ""
            for input_item in message.content:
                if input_item.type == "text":
                    text_content += input_item.text
                elif input_item.type == "image_url":
                    text_content += IMAGE_PLACEHOLDER
                    image_url = input_item.image_url.url
                    if re.match(r"^data:image\/(png|jpg|jpeg|gif|bmp);base64,(.+)$", image_url):  # base64 image
                        image_stream = io.BytesIO(base64.b64decode(image_url.split(",", maxsplit=1)[1]))
                    elif os.path.isfile(image_url):  # local file
                        _check_lfi_path(image_url)
                        image_stream = open(image_url, "rb")
                    else:  # web uri
                        _check_ssrf_url(image_url)
                        image_stream = requests.get(image_url, stream=True).raw

                    images.append(Image.open(image_stream).convert("RGB"))
                elif input_item.type == "video_url":
                    text_content += VIDEO_PLACEHOLDER
                    video_url = input_item.video_url.url
                    if re.match(r"^data:video\/(mp4|mkv|avi|mov);base64,(.+)$", video_url):  # base64 video
                        video_stream = io.BytesIO(base64.b64decode(video_url.split(",", maxsplit=1)[1]))
                    elif os.path.isfile(video_url):  # local file
                        _check_lfi_path(video_url)
                        video_stream = video_url
                    else:  # web uri
                        _check_ssrf_url(video_url)
                        video_stream = requests.get(video_url, stream=True).raw

                    videos.append(video_stream)
                elif input_item.type == "audio_url":
                    text_content += AUDIO_PLACEHOLDER
                    audio_url = input_item.audio_url.url
                    if re.match(r"^data:audio\/(mpeg|mp3|wav|ogg);base64,(.+)$", audio_url):  # base64 audio
                        audio_stream = io.BytesIO(base64.b64decode(audio_url.split(",", maxsplit=1)[1]))
                    elif os.path.isfile(audio_url):  # local file
                        _check_lfi_path(audio_url)
                        audio_stream = audio_url
                    else:  # web uri
                        _check_ssrf_url(audio_url)
                        audio_stream = requests.get(audio_url, stream=True).raw

                    audios.append(audio_stream)
                else:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid input type {input_item.type}."
                    )

            input_messages.append({"role": ROLE_MAPPING[message.role], "content": text_content})
        else:
            input_messages.append({"role": ROLE_MAPPING[message.role], "content": message.content})

    tool_list = request.tools
    if isinstance(tool_list, list) and len(tool_list):
        try:
            tools = json.dumps([dictify(tool.function) for tool in tool_list], ensure_ascii=False)
        except json.JSONDecodeError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid tools")
    else:
        tools = None

    return input_messages, system, tools, images or None, videos or None, audios or None


def _create_stream_chat_completion_chunk(
    completion_id: str,
    model: str,
    delta: "ChatCompletionMessage",
    index: Optional[int] = 0,
    finish_reason: Optional["Finish"] = None,
) -> str:
    choice_data = ChatCompletionStreamResponseChoice(index=index, delta=delta, finish_reason=finish_reason)
    chunk = ChatCompletionStreamResponse(id=completion_id, model=model, choices=[choice_data])
    return jsonify(chunk)


async def create_chat_completion_response(
    request: "ChatCompletionRequest", chat_model: "ChatModel"
) -> "ChatCompletionResponse":
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    input_messages, system, tools, images, videos, audios = _process_request(request)
    responses = await chat_model.achat(
        input_messages,
        system,
        tools,
        images,
        videos,
        audios,
        do_sample=request.do_sample,
        temperature=request.temperature,
        top_p=request.top_p,
        max_new_tokens=request.max_tokens,
        num_return_sequences=request.n,
        repetition_penalty=request.presence_penalty,
        stop=request.stop,
    )

    prompt_length, response_length = 0, 0
    choices = []
    for i, response in enumerate(responses):
        if tools:
            result = chat_model.engine.template.extract_tool(response.response_text)
        else:
            result = response.response_text

        if isinstance(result, list):
            tool_calls = []
            for tool in result:
                function = Function(name=tool.name, arguments=tool.arguments)
                tool_calls.append(FunctionCall(id=f"call_{uuid.uuid4().hex}", function=function))

            response_message = ChatCompletionMessage(role=Role.ASSISTANT, tool_calls=tool_calls)
            finish_reason = Finish.TOOL
        else:
            response_message = ChatCompletionMessage(role=Role.ASSISTANT, content=result)
            finish_reason = Finish.STOP if response.finish_reason == "stop" else Finish.LENGTH

        choices.append(ChatCompletionResponseChoice(index=i, message=response_message, finish_reason=finish_reason))
        prompt_length = response.prompt_length
        response_length += response.response_length

    usage = ChatCompletionResponseUsage(
        prompt_tokens=prompt_length,
        completion_tokens=response_length,
        total_tokens=prompt_length + response_length,
    )

    return ChatCompletionResponse(id=completion_id, model=request.model, choices=choices, usage=usage)


async def create_stream_chat_completion_response(
    request: "ChatCompletionRequest", chat_model: "ChatModel"
) -> AsyncGenerator[str, None]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    input_messages, system, tools, images, videos, audios = _process_request(request)
    if tools:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot stream function calls.")

    if request.n > 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot stream multiple responses.")

    yield _create_stream_chat_completion_chunk(
        completion_id=completion_id, model=request.model, delta=ChatCompletionMessage(role=Role.ASSISTANT, content="")
    )
    async for new_token in chat_model.astream_chat(
        input_messages,
        system,
        tools,
        images,
        videos,
        audios,
        do_sample=request.do_sample,
        temperature=request.temperature,
        top_p=request.top_p,
        max_new_tokens=request.max_tokens,
        repetition_penalty=request.presence_penalty,
        stop=request.stop,
    ):
        if len(new_token) != 0:
            yield _create_stream_chat_completion_chunk(
                completion_id=completion_id, model=request.model, delta=ChatCompletionMessage(content=new_token)
            )

    yield _create_stream_chat_completion_chunk(
        completion_id=completion_id, model=request.model, delta=ChatCompletionMessage(), finish_reason=Finish.STOP
    )
    yield "[DONE]"


async def create_score_evaluation_response(
    request: "ScoreEvaluationRequest", chat_model: "ChatModel"
) -> "ScoreEvaluationResponse":
    score_id = f"scoreval-{uuid.uuid4().hex}"
    if len(request.messages) == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request")

    scores = await chat_model.aget_scores(request.messages, max_length=request.max_length)
    return ScoreEvaluationResponse(id=score_id, model=request.model, scores=scores)
