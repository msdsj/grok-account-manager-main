"""AWS Event Stream 二进制分帧解析（Kiro / CodeWhisperer 流式响应）。

每帧结构：total_length(4) + headers_length(4) + prelude_crc(4) + headers + payload + message_crc(4)。
所有长度与 CRC 均为大端序，CRC 使用标准 CRC32。
"""

from __future__ import annotations

import json
import struct
import uuid
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

PRELUDE_LENGTH = 12
MESSAGE_CRC_LENGTH = 4
MIN_MESSAGE_LENGTH = PRELUDE_LENGTH + MESSAGE_CRC_LENGTH

_HEADER_VALUE_BYTE = 2
_HEADER_VALUE_SHORT = 3
_HEADER_VALUE_INTEGER = 4
_HEADER_VALUE_LONG = 5
_HEADER_VALUE_BYTE_ARRAY = 6
_HEADER_VALUE_STRING = 7
_HEADER_VALUE_TIMESTAMP = 8
_HEADER_VALUE_UUID = 9

_FIXED_VALUE_FORMATS = {
    _HEADER_VALUE_BYTE: (">b", 1),
    _HEADER_VALUE_SHORT: (">h", 2),
    _HEADER_VALUE_INTEGER: (">i", 4),
    _HEADER_VALUE_LONG: (">q", 8),
    _HEADER_VALUE_TIMESTAMP: (">q", 8),
}


class EventStreamError(ValueError):
    """Event Stream 分帧数据非法。"""


class IncompleteMessage(EventStreamError):
    """缓冲区中还没有完整的一帧。"""


@dataclass(frozen=True)
class EventMessage:
    headers: dict[str, Any]
    payload: bytes

    @property
    def event_type(self) -> str:
        return str(self.headers.get(":event-type") or "")

    def json(self) -> Any:
        if not self.payload:
            return {}
        return json.loads(self.payload.decode("utf-8"))


def _read(data: bytes, offset: int, size: int) -> tuple[bytes, int]:
    end = offset + size
    if end > len(data):
        raise EventStreamError("Event Stream 头部字段被截断")
    return data[offset:end], end


def _decode_header_value(data: bytes, offset: int, value_type: int) -> tuple[Any, int]:
    if value_type == 0:
        return True, offset
    if value_type == 1:
        return False, offset
    fixed = _FIXED_VALUE_FORMATS.get(value_type)
    if fixed is not None:
        fmt, size = fixed
        raw, offset = _read(data, offset, size)
        return struct.unpack(fmt, raw)[0], offset
    if value_type in {_HEADER_VALUE_BYTE_ARRAY, _HEADER_VALUE_STRING}:
        raw_length, offset = _read(data, offset, 2)
        (length,) = struct.unpack(">H", raw_length)
        raw, offset = _read(data, offset, length)
        if value_type == _HEADER_VALUE_STRING:
            return raw.decode("utf-8", "replace"), offset
        return raw, offset
    if value_type == _HEADER_VALUE_UUID:
        raw, offset = _read(data, offset, 16)
        return str(uuid.UUID(bytes=raw)), offset
    raise EventStreamError(f"未知的 Event Stream 头部类型: {value_type}")


def _decode_headers(data: bytes) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    offset = 0
    while offset < len(data):
        raw_length, offset = _read(data, offset, 1)
        name_length = raw_length[0]
        raw_name, offset = _read(data, offset, name_length)
        name = raw_name.decode("utf-8", "replace")
        raw_type, offset = _read(data, offset, 1)
        value, offset = _decode_header_value(data, offset, raw_type[0])
        headers[name] = value
    return headers


def parse_message(data: bytes, *, validate_crc: bool = True) -> tuple[EventMessage, int]:
    """解析一帧，返回 (消息, 已消费字节数)；数据不完整时抛 IncompleteMessage。"""
    if len(data) < PRELUDE_LENGTH:
        raise IncompleteMessage("Event Stream 前导长度不足")
    total_length, headers_length, prelude_crc = struct.unpack(">III", data[:PRELUDE_LENGTH])
    if total_length < MIN_MESSAGE_LENGTH:
        raise EventStreamError(f"Event Stream 帧长度非法: {total_length}")
    if len(data) < total_length:
        raise IncompleteMessage("Event Stream 帧尚未接收完整")
    if headers_length > total_length - MIN_MESSAGE_LENGTH:
        raise EventStreamError("Event Stream 头部长度超出帧范围")

    if validate_crc:
        expected_prelude_crc = zlib.crc32(data[:8]) & 0xFFFFFFFF
        if expected_prelude_crc != prelude_crc:
            raise EventStreamError("Event Stream prelude CRC 校验失败")
        (message_crc,) = struct.unpack(">I", data[total_length - MESSAGE_CRC_LENGTH : total_length])
        expected_message_crc = zlib.crc32(data[: total_length - MESSAGE_CRC_LENGTH]) & 0xFFFFFFFF
        if expected_message_crc != message_crc:
            raise EventStreamError("Event Stream message CRC 校验失败")

    headers_start = PRELUDE_LENGTH
    headers_end = headers_start + headers_length
    headers = _decode_headers(data[headers_start:headers_end])
    payload = data[headers_end : total_length - MESSAGE_CRC_LENGTH]
    return EventMessage(headers=headers, payload=payload), total_length


def iter_messages(chunks: Iterable[bytes], *, validate_crc: bool = True) -> Iterator[EventMessage]:
    """把网络分块输入切成完整消息；跨块边界的帧会被正确拼接。"""
    buffer = b""
    for chunk in chunks:
        if not chunk:
            continue
        buffer += chunk
        while len(buffer) >= PRELUDE_LENGTH:
            (total_length,) = struct.unpack(">I", buffer[:4])
            if total_length < MIN_MESSAGE_LENGTH:
                raise EventStreamError(f"Event Stream 帧长度非法: {total_length}")
            if len(buffer) < total_length:
                break
            message, consumed = parse_message(buffer[:total_length], validate_crc=validate_crc)
            buffer = buffer[consumed:]
            yield message
    if buffer:
        raise EventStreamError("Event Stream 在消息中途结束")
