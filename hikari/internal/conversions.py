#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright © Nekokatt 2019-2020
#
# This file is part of Hikari.
#
# Hikari is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Hikari is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Hikari. If not, see <https://www.gnu.org/licenses/>.
"""Basic transformation utilities."""
__all__ = [
    "nullable_cast",
    "try_cast",
    "try_cast_or_defer_unary_operator",
    "put_if_specified",
    "image_bytes_to_image_data",
    "parse_http_date",
    "parse_iso_8601_ts",
    "discord_epoch_to_datetime",
    "unix_epoch_to_datetime",
    "pluralize",
    "snoop_typehint_from_scope",
]

import base64
import contextlib
import datetime
import email.utils
import enum
import functools
import operator
import re
import types
import typing

IntFlagT = typing.TypeVar("IntFlagT", bound=enum.IntFlag)
RawIntFlagValueT = typing.Union[typing.AnyStr, typing.SupportsInt, int]
DISCORD_EPOCH: typing.Final[int] = 1_420_070_400
ISO_8601_DATE_PART: typing.Final[typing.Pattern] = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
ISO_8601_TIME_PART: typing.Final[typing.Pattern] = re.compile(r"T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?", re.I)
ISO_8601_TZ_PART: typing.Final[typing.Pattern] = re.compile(r"([+-])(\d{2}):(\d{2})$")

CastInputT = typing.TypeVar("CastInputT")
CastOutputT = typing.TypeVar("CastOutputT")
DefaultT = typing.TypeVar("DefaultT")
TypeCastT = typing.Callable[[CastInputT], CastOutputT]
ResultT = typing.Union[CastOutputT, DefaultT]


def nullable_cast(value: CastInputT, cast: TypeCastT, /) -> ResultT:
    """Attempt to cast the given `value` with the given `cast`.

    This will only succeed if `value` is not `None`. If it is `None`, then
    `None` is returned instead.
    """
    if value is None:
        return None
    return cast(value)


def try_cast(value: CastInputT, cast: TypeCastT, default: DefaultT = None, /) -> ResultT:
    """Try to cast the given value to the given cast.

    If it throws a `Exception` or derivative, it will return `default`
    instead of the cast value instead.
    """
    with contextlib.suppress(Exception):
        return cast(value)
    return default


def try_cast_or_defer_unary_operator(type_: typing.Type, /):
    """Return a unary operator that will try to cast the given input to the type provided.

    Parameters
    ----------
    type_ : typing.Callable[..., `output type`]
        The type to cast to.
    """
    return lambda data: try_cast(data, type_, data)


def put_if_specified(
    mapping: typing.Dict[typing.Hashable, typing.Any],
    key: typing.Hashable,
    value: typing.Any,
    type_after: typing.Optional[TypeCastT] = None,
    /,
) -> None:
    """Add a value to the mapping under the given key as long as the value is not `...`.

    Parameters
    ----------
    mapping : typing.Dict[typing.Hashable, typing.Any]
        The mapping to add to.
    key : typing.Hashable
        The key to add the value under.
    value : typing.Any
        The value to add.
    type_after : typing.Callable[[`input type`], `output type`], optional
        Type to apply to the value when added.
    """
    if value is not ...:
        if type_after:
            mapping[key] = type_after(value)
        else:
            mapping[key] = value


def image_bytes_to_image_data(img_bytes: typing.Optional[bytes] = None, /) -> typing.Optional[str]:
    """Encode image bytes into an image data string.

    Parameters
    ----------
    img_bytes : bytes, optional
        The image bytes.

    Raises
    ------
    ValueError
        If the image type passed is not supported.

    Returns
    -------
    str, optional
        The `image_bytes` given encoded into an image data string or
        `None`.

    !!! note
        Supported image types: `.png`, `.jpeg`, `.jfif`, `.gif`, `.webp`
    """
    if img_bytes is None:
        return None

    if img_bytes[:8] == b"\211PNG\r\n\032\n":
        img_type = "image/png"
    elif img_bytes[6:10] in (b"Exif", b"JFIF"):
        img_type = "image/jpeg"
    elif img_bytes[:6] in (b"GIF87a", b"GIF89a"):
        img_type = "image/gif"
    elif img_bytes.startswith(b"RIFF") and img_bytes[8:12] == b"WEBP":
        img_type = "image/webp"
    else:
        raise ValueError("Unsupported image type passed")

    image_data = base64.b64encode(img_bytes).decode()

    return f"data:{img_type};base64,{image_data}"


def parse_http_date(date_str: str, /) -> datetime.datetime:
    """Return the HTTP date as a datetime object.

    Parameters
    ----------
    date_str : str
        The RFC-2822 (section 3.3) compliant date string to parse.

    Returns
    -------
    datetime.datetime
        The HTTP date as a datetime object.

    References
    ----------
    [RFC 2822](https://www.ietf.org/rfc/rfc2822.txt)
    """
    return email.utils.parsedate_to_datetime(date_str)


def parse_iso_8601_ts(date_string: str, /) -> datetime.datetime:
    """Parse an ISO 8601 date string into a `datetime.datetime` object.

    Parameters
    ----------
    date_string : str
        The ISO 8601 compliant date string to parse.

    Returns
    -------
    datetime.datetime
        The ISO 8601 date string as a datetime object.

    References
    ----------
    [ISO 8601](https://en.wikipedia.org/wiki/ISO_8601)
    """
    year, month, day = map(int, ISO_8601_DATE_PART.findall(date_string)[0])

    time_part = ISO_8601_TIME_PART.findall(date_string)[0]
    hour, minute, second, partial = time_part

    # Pad the millisecond part if it is not in microseconds, otherwise Python will complain.
    partial = partial + (6 - len(partial)) * "0"
    hour, minute, second, partial = int(hour), int(minute), int(second), int(partial)
    if date_string.endswith(("Z", "z")):
        timezone = datetime.timezone.utc
    else:
        sign, tz_hour, tz_minute = ISO_8601_TZ_PART.findall(date_string)[0]
        tz_hour, tz_minute = int(tz_hour), int(tz_minute)
        offset = datetime.timedelta(hours=tz_hour, minutes=tz_minute)
        if sign == "-":
            offset = -offset
        timezone = datetime.timezone(offset)

    return datetime.datetime(year, month, day, hour, minute, second, partial, timezone)


def discord_epoch_to_datetime(epoch: int, /) -> datetime.datetime:
    """Parse a Discord epoch into a `datetime.datetime` object.

    Parameters
    ----------
    epoch : int
        Number of milliseconds since 1/1/2015 (UTC)

    Returns
    -------
    datetime.datetime
        Number of seconds since 1/1/1970 within a datetime object (UTC).
    """
    return datetime.datetime.fromtimestamp(epoch / 1000 + DISCORD_EPOCH, datetime.timezone.utc)


def unix_epoch_to_datetime(epoch: int, /) -> datetime.datetime:
    """Parse a UNIX epoch to a `datetime.datetime` object.

    Parameters
    ----------
    epoch : int
        Number of milliseconds since 1/1/1970 (UTC)

    Returns
    -------
    datetime.datetime
        Number of seconds since 1/1/1970 within a datetime object (UTC).
    """
    return datetime.datetime.fromtimestamp(epoch / 1000, datetime.timezone.utc)


def pluralize(count: int, name: str, suffix: str = "s") -> str:
    """Pluralizes a word."""
    return f"{count} {name + suffix}" if count - 1 else f"{count} {name}"


def snoop_typehint_from_scope(frame: types.FrameType, typehint: typing.Union[str, typing.Any]) -> typing.Any:
    """Resolve a string type hint from a given stack frame.

    This snoops around the local and global scope for the given frame to find
    the given attribute name, taking into account nested function calls. The
    reason to do this is that if a string type hint is used, or the
    `from __future__ import annotations` directive is used, the physical thing
    that the type hint represents will no longer be evaluated by the
    interpreter. This is an implementation that does not require the use of
    `eval`, and thus reduces the risk of arbitrary code execution as a
    result.

    Nested parameters such as `typing.Sequence` should also be able to be
    resolved correctly.

    Parameters
    ----------
    frame : types.FrameType
        The stack frame that the element with the typehint was defined in.
        This is retrieved using `inspect.stack` `(frame_no)[0][0]`,
        where `frame_no` is the number of frames from this invocation that
        you want to snoop the scope at.
    typehint : typing.Union[str, typing.Any]
        The type hint to resolve. If a non-`str` is passed, then this is
        returned immediately as the result.

    Returns
    -------
    typing.Any
        The physical representation of the given type hint.

    Raises
    ------
    NameError
        If the attribute was not found.

    !!! warning
        The input frame must be manually dereferenced using the `del` keyword
        after use. Any functions that are decorated and wrapped when using this
        lookup must use `functools.wraps` to ensure that the correct scope is
        identified on the stack.

        This is incredibly unpythonic and baremetal, but due to
        [PEP 563](https://www.python.org/dev/peps/pep-0563/) there is no other
        consistent way of making this work correctly.
    """
    if not isinstance(typehint, str):
        return typehint

    fragments = typehint.split(".")

    for scope in (frame.f_locals, frame.f_globals):
        try:
            scope = scope[fragments[0]]
            for attr in fragments[1:]:
                scope = getattr(scope, attr)
            return scope
        except (AttributeError, KeyError):
            pass
    raise NameError(f"No attribute {typehint} was found in enclosing scope")


def dereference_int_flag(
    int_flag_type: typing.Type[IntFlagT],
    raw_value: typing.Union[RawIntFlagValueT, typing.Collection[RawIntFlagValueT]],
) -> IntFlagT:
    """Cast to the provided `enum.IntFlag` type.

    This supports resolving bitfield integers as well as decoding a sequence
    of case insensitive flag names into one combined value.

    Parameters
    ----------
    int_flag_type : typing.Type[enum.IntFlag]
        The type of the int flag to check.
    raw_value : Castable Value
        The raw value to convert.

    Returns
    -------
    enum.IntFlag
        The cast value as a flag.

    !!! note
        Types that are a `Castable Value` include:
        - `str`
        - `int`
        - `typing.SupportsInt`
        - `typing.Collection`[`Castable Value`]

        When a collection is passed, values will be combined using functional
        reduction via the `operator.or_` operator.
    """
    if isinstance(raw_value, str) and raw_value.isdigit():
        raw_value = int(raw_value)

    if not isinstance(raw_value, int):
        raw_value = functools.reduce(operator.or_, (int_flag_type[name.upper()] for name in raw_value))

    return int_flag_type(raw_value)
