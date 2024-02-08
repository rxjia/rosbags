# Copyright 2020 - 2024 Ternaris
# SPDX-License-Identifier: Apache-2.0
"""MSG Parser.

Grammar, parse tree visitor and conversion functions for message definitions in
`MSG`_ format. It also supports concatened message definitions as found in
Rosbag1 connection information.

.. _MSG: http://wiki.ros.org/msg

"""

from __future__ import annotations

import re
from enum import IntEnum, auto
from hashlib import md5
from pathlib import PurePosixPath as Path
from typing import TYPE_CHECKING, cast

from rosbags.interfaces import Nodetype

from . import types
from .base import TypesysError, normalize_fieldname, parse_message_definition
from .peg import Rule, Visitor, parse_grammar

if TYPE_CHECKING:
    import sys
    from typing import ClassVar, Literal, TypeVar

    if sys.version_info >= (3, 10):
        from typing import TypeAlias
    else:
        from typing_extensions import TypeAlias

    from rosbags.interfaces.typing import (
        BaseDesc,
        Basename,
        Constdefs,
        ConstValue,
        Fielddefs,
        FieldDesc,
        NameDesc,
        Typesdict,
        Typestore,
    )

    T = TypeVar('T')

    L: TypeAlias = 'tuple[Literal["LITERAL"], str]'

    Const: TypeAlias = 'tuple[Literal[Node.CONST], tuple[str, Basename, ConstValue]]'
    Field: TypeAlias = 'tuple[Literal[Node.FIELD], tuple[str, FieldDesc]]'
    Msgdesc: TypeAlias = 'tuple[Const | Field, ...]'

GRAMMAR_MSG = r"""
specification
  = msgdef (msgsep msgdef)*

msgdef
  = r'MSG:\s' scoped_name definition*

msgsep
  = r'================================================================================'

definition
  = const_dcl
  / field_dcl

const_dcl
  = 'string' identifier '=' r'(?!={79}\n)[^\n]+'
  / type_spec identifier '=' float_literal
  / type_spec identifier '=' integer_literal
  / type_spec identifier '=' boolean_literal

field_dcl
  = type_spec identifier default_value?

type_spec
  = array_type_spec
  / bounded_array_type_spec
  / simple_type_spec

array_type_spec
  = simple_type_spec array_size

bounded_array_type_spec
  = simple_type_spec array_bounds

simple_type_spec
  = 'string' '<=' integer_literal
  / scoped_name

array_size
  = '[' integer_literal? ']'

array_bounds
  = '[<=' integer_literal ']'

scoped_name
  = identifier '/' scoped_name
  / identifier

identifier
  = r'[a-zA-Z_][a-zA-Z_0-9]*'

default_value
  = literal

literal
  = float_literal
  / integer_literal
  / boolean_literal
  / string_literal
  / array_literal

boolean_literal
  = r'[tT][rR][uU][eE]'
  / r'[fF][aA][lL][sS][eE]'
  / '0'
  / '1'

integer_literal
  = hexadecimal_literal
  / octal_literal
  / decimal_literal

decimal_literal
  = r'[-+]?[1-9][0-9]+'
  / r'[-+]?[0-9]'

octal_literal
  = r'[-+]?0[0-7]+'

hexadecimal_literal
  = r'[-+]?0[xX][a-fA-F0-9]+'

float_literal
  = r'[-+]?[0-9]*\.[0-9]+([eE][-+]?[0-9]+)?'
  / r'[-+]?[0-9]*\.?[0-9]+([eE][-+]?[0-9]+)'

string_literal
  = '"' r'(\\"|[^"])*' '"'
  / '\'' r'(\\\'|[^'])*' '\''

array_literal
  = '[' array_elements? ']'

array_elements
  = literal ',' array_elements
  / literal
"""


def normalize_msgtype(name: str) -> str:
    """Normalize message typename.

    Args:
        name: Message typename.

    Returns:
        Normalized name.

    """
    path = Path(name)
    if path.parent.name != 'msg':
        path = path.parent / 'msg' / path.name
    return str(path)


def normalize_fieldtype(typename: str, field: FieldDesc, names: list[str]) -> FieldDesc:
    """Normalize field typename.

    Args:
        typename: Type name of field owner.
        field: Field definition.
        names: Valid message names.

    Returns:
        Normalized fieldtype.

    """
    if field[0] == Nodetype.BASE:
        return field

    dct = {Path(name).name: name for name in names}
    ftype, args = field
    ifield = field if ftype == Nodetype.NAME else args[0]

    if ifield[0] == Nodetype.BASE:
        return field

    assert isinstance(ifield, tuple)
    assert ifield[0] == Nodetype.NAME

    name = ifield[1]
    if name in dct:
        name = dct[name]
    elif name == 'Header':
        name = 'std_msgs/msg/Header'
    elif '/' not in name:
        name = str(Path(typename).parent / name)
    elif '/msg/' not in name:
        name = str((path := Path(name)).parent / 'msg' / path.name)
    ifield = Nodetype.NAME, name

    return ifield if ftype == Nodetype.NAME else (ftype, (ifield, args[1]))  # type: ignore[return-value]


def denormalize_msgtype(typename: str) -> str:
    """Undo message tyoename normalization.

    Args:
        typename: Normalized message typename.

    Returns:
        ROS1 style name.

    """
    assert '/msg/' in typename
    return str((path := Path(typename)).parent.parent / path.name)


class Node(IntEnum):
    """Parse tree node types."""

    CONST = auto()
    FIELD = auto()


class VisitorMSG(Visitor):
    """MSG file visitor."""

    RULES = parse_grammar(GRAMMAR_MSG, re.compile(r'(\s|#[^\n]*$)+', re.M | re.S))

    BASETYPES: ClassVar[set[str]] = {
        'bool',
        'octet',
        'int8',
        'int16',
        'int32',
        'int64',
        'uint8',
        'uint16',
        'uint32',
        'uint64',
        'float32',
        'float64',
        'string',
    }

    def visit_specification(
        self,
        children: tuple[tuple[str, Msgdesc], tuple[tuple[str, tuple[str, Msgdesc]], ...]],
    ) -> Typesdict:
        """Process start symbol."""
        typelist = [children[0], *[x[1] for x in children[1]]]
        typedict = dict(typelist)
        names = list(typedict.keys())
        res: Typesdict = {}
        for name, items in typedict.items():
            consts: Constdefs = []
            fields: Fielddefs = []

            for item in items:
                if item[0] == Node.CONST:
                    consts.append(item[1])
                else:
                    assert item[0] == Node.FIELD
                    fields.append((item[1][0], normalize_fieldtype(name, item[1][1], names)))

            res[name] = consts, fields
        return res

    def visit_msgdef(self, children: tuple[str, NameDesc, Msgdesc]) -> tuple[str, Msgdesc]:
        """Process single message definition."""
        return normalize_msgtype(children[1][1]), children[2]

    def visit_msgsep(self, _: str) -> None:
        """Process message separator, suppress output."""

    def visit_const_dcl(self, children: tuple[BaseDesc | L, NameDesc, L, ConstValue]) -> Const:
        """Process const declaration."""
        typ: Basename
        value = children[3]
        if children[0][0] == 'LITERAL':
            assert isinstance(value, str)
            value = value.strip()
            typ = 'string'
        else:
            assert not isinstance(children[3], str)
            typ = children[0][1][0]
        return Node.CONST, (normalize_fieldname(children[1][1]), typ, value)

    def visit_field_dcl(
        self,
        children: tuple[FieldDesc, NameDesc, tuple[ConstValue, ...]],
    ) -> Field:
        """Process field declaration."""
        return Node.FIELD, (normalize_fieldname(children[1][1]), children[0])

    def visit_array_type_spec(
        self,
        children: tuple[BaseDesc | NameDesc, tuple[L, tuple[int, ...], L]],
    ) -> FieldDesc:
        """Process array type specifier."""
        if length := children[1][1]:
            return Nodetype.ARRAY, (children[0], length[0])
        return Nodetype.SEQUENCE, (children[0], 0)

    def visit_bounded_array_type_spec(
        self,
        children: tuple[BaseDesc | NameDesc, tuple[L, int, L]],
    ) -> FieldDesc:
        """Process bounded array type specifier."""
        return Nodetype.SEQUENCE, (children[0], children[1][1])

    def visit_simple_type_spec(self, children: NameDesc | tuple[L, L, int]) -> BaseDesc | NameDesc:
        """Process simple type specifier."""
        if len(children) == 3:
            assert children[1] == (Rule.LIT, '<=')
            assert isinstance(children[2], int)
            return Nodetype.BASE, ('string', children[2])
        typespec = children[1]
        assert isinstance(typespec, str)
        dct: dict[str, str] = {
            'time': 'builtin_interfaces/msg/Time',
            'duration': 'builtin_interfaces/msg/Duration',
            'byte': 'octet',
            'char': 'uint8',
        }
        typespec = dct.get(typespec, typespec)
        if typespec in VisitorMSG.BASETYPES:
            return Nodetype.BASE, (cast('Basename', typespec), 0)
        return Nodetype.NAME, typespec

    def visit_scoped_name(self, children: NameDesc | tuple[NameDesc, L, NameDesc]) -> NameDesc:
        """Process scoped name."""
        if len(children) == 2:
            return children
        return Nodetype.NAME, f'{children[0][1]}/{children[2][1]}'

    def visit_identifier(self, children: str) -> NameDesc:
        """Process identifier."""
        return Nodetype.NAME, children

    def visit_boolean_literal(self, children: str) -> bool:
        """Process boolean literal."""
        return children.lower() in {'true', '1'}

    def visit_float_literal(self, children: str) -> float:
        """Process float literal."""
        return float(children)

    def visit_decimal_literal(self, children: str) -> int:
        """Process decimal integer literal."""
        return int(children)

    def visit_octal_literal(self, children: str) -> int:
        """Process octal integer literal."""
        return int(children, 8)

    def visit_hexadecimal_literal(self, children: str) -> int:
        """Process hexadecimal integer literal."""
        return int(children, 16)

    def visit_string_literal(self, children: str) -> str:
        """Process integer literal."""
        return children[1]


def get_types_from_msg(text: str, name: str) -> Typesdict:
    """Get type from msg message definition.

    Args:
        text: Message definiton.
        name: Message typename.

    Returns:
        list with single message name and parsetree.

    """
    return parse_message_definition(VisitorMSG(), f'MSG: {name}\n{text}')


def gendefhash(
    typename: str,
    subdefs: dict[str, tuple[str, str]],
    typestore: Typestore = types,
    ros_version: int = 1,
) -> tuple[str, str]:
    """Generate message definition and hash for type.

    The subdefs argument will be filled with child definitions.

    Args:
        typename: Name of type to generate definition for.
        subdefs: Child definitions.
        typestore: Custom type store.
        ros_version: ROS version number.

    Returns:
        Message definition and hash.

    Raises:
        TypesysError: Type does not exist.

    """
    typemap = (
        {'builtin_interfaces/msg/Time': 'time', 'builtin_interfaces/msg/Duration': 'duration'}
        if ros_version == 1
        else {}
    )

    deftext: list[str] = []
    hashtext: list[str] = []
    if typename not in typestore.FIELDDEFS:
        msg = f'Type {typename!r} is unknown.'
        raise TypesysError(msg)

    for name, typ, value in typestore.FIELDDEFS[typename][0]:
        stripped_name = name.rstrip('_')
        deftext.append(f'{typ} {stripped_name}={value}')
        hashtext.append(f'{typ} {stripped_name}={value}')

    for name, desc in typestore.FIELDDEFS[typename][1]:
        if name == 'structure_needs_at_least_one_member':
            continue
        stripped_name = name.rstrip('_')
        if desc[0] == Nodetype.BASE:
            argname, arglimit = desc[1]
            if argname == 'octet':
                argname = 'byte'  # type: ignore[assignment]
            elif argname == 'string':
                argname = f'string<={arglimit}' if arglimit else 'string'  # type: ignore[assignment]
            deftext.append(f'{argname} {stripped_name}')
            hashtext.append(f'{argname} {stripped_name}')
        elif desc[0] == int(Nodetype.NAME):
            args = desc[1]
            assert isinstance(args, str)
            subname = args
            if subname in typemap:
                deftext.append(f'{typemap[subname]} {stripped_name}')
                hashtext.append(f'{typemap[subname]} {stripped_name}')
            else:
                if subname not in subdefs:
                    subdefs[subname] = ('', '')
                    subdefs[subname] = gendefhash(subname, subdefs, typestore, ros_version)
                deftext.append(f'{denormalize_msgtype(subname)} {stripped_name}')
                hashtext.append(f'{subdefs[subname][1]} {stripped_name}')
        else:
            assert desc[0] in {Nodetype.ARRAY, Nodetype.SEQUENCE}
            assert isinstance(desc[1], tuple)
            subdesc, num = desc[1]
            isubtype, isubname = subdesc
            count = '' if num == 0 else str(num) if desc[0] == int(Nodetype.ARRAY) else f'<={num}'
            if isubtype == int(Nodetype.BASE):
                if isubname[0] == 'octet':
                    isubname = ('byte', 0)  # type: ignore[assignment]
                elif isubname[0] == 'string':
                    isubname = (f'string<={isubname[1]}' if isubname[1] else 'string', 0)  # type: ignore[assignment]
                deftext.append(f'{isubname[0]}[{count}] {stripped_name}')
                hashtext.append(f'{isubname[0]}[{count}] {stripped_name}')
            elif isubname in typemap:
                assert isinstance(isubname, str)
                deftext.append(f'{typemap[isubname]}[{count}] {stripped_name}')
                hashtext.append(f'{typemap[isubname]}[{count}] {stripped_name}')
            else:
                assert isinstance(isubname, str)
                if isubname not in subdefs:
                    subdefs[isubname] = ('', '')
                    subdefs[isubname] = gendefhash(isubname, subdefs, typestore, ros_version)
                deftext.append(f'{denormalize_msgtype(isubname)}[{count}] {stripped_name}')
                hashtext.append(f'{subdefs[isubname][1]} {stripped_name}')

    if ros_version == 1 and typename == 'std_msgs/msg/Header':
        deftext.insert(0, 'uint32 seq')
        hashtext.insert(0, 'uint32 seq')

    deftext.append('')
    return '\n'.join(deftext), md5('\n'.join(hashtext).encode()).hexdigest()  # noqa: S324


def generate_msgdef(
    typename: str,
    typestore: Typestore = types,
    ros_version: int = 1,
) -> tuple[str, str]:
    """Generate message definition for type.

    Args:
        typename: Name of type to generate definition for.
        typestore: Custom type store.
        ros_version: ROS version number.

    Returns:
        Message definition.

    """
    subdefs: dict[str, tuple[str, str]] = {}
    msgdef, md5sum = gendefhash(typename, subdefs, typestore, ros_version)

    msgdef = ''.join(
        [
            msgdef,
            *[f'{"=" * 80}\nMSG: {denormalize_msgtype(k)}\n{v[0]}' for k, v in subdefs.items()],
        ],
    )

    return msgdef, md5sum
