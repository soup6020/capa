# Copyright 2023 Google LLC
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

from typing import Iterator

from binaryninja import Segment, BinaryView, SymbolType, SymbolBinding

import capa.features.extractors.common
import capa.features.extractors.helpers
import capa.features.extractors.strings
from capa.features.file import Export, Import, Section, FunctionName
from capa.features.common import (
    FORMAT_PE,
    FORMAT_ELF,
    FORMAT_SC32,
    FORMAT_SC64,
    FORMAT_BINJA_DB,
    Format,
    String,
    Feature,
    Characteristic,
)
from capa.features.address import NO_ADDRESS, Address, FileOffsetAddress, AbsoluteVirtualAddress
from capa.features.extractors.binja.helpers import read_c_string, unmangle_c_name


def check_segment_for_pe(bv: BinaryView, seg: Segment) -> Iterator[tuple[Feature, Address]]:
    """check segment for embedded PE"""
    start = 0
    if bv.view_type == "PE" and seg.start == bv.start:
        # If this is the first segment of the binary, skip the first bytes.
        # Otherwise, there will always be a matched PE at the start of the binaryview.
        start += 1

    buf = bv.read(seg.start, seg.length)

    for offset, _ in capa.features.extractors.helpers.carve_pe(buf, start):
        yield Characteristic("embedded pe"), FileOffsetAddress(seg.start + offset)


def extract_file_embedded_pe(bv: BinaryView) -> Iterator[tuple[Feature, Address]]:
    """extract embedded PE features"""
    for seg in bv.segments:
        yield from check_segment_for_pe(bv, seg)


def extract_file_export_names(bv: BinaryView) -> Iterator[tuple[Feature, Address]]:
    """extract function exports"""
    for sym in bv.get_symbols_of_type(SymbolType.FunctionSymbol) + bv.get_symbols_of_type(SymbolType.DataSymbol):
        if sym.binding in [SymbolBinding.GlobalBinding, SymbolBinding.WeakBinding]:
            name = sym.short_name
            if name.startswith("__forwarder_name(") and name.endswith(")"):
                yield Export(name[17:-1]), AbsoluteVirtualAddress(sym.address)
                yield Characteristic("forwarded export"), AbsoluteVirtualAddress(sym.address)
            else:
                yield Export(name), AbsoluteVirtualAddress(sym.address)

                unmangled_name = unmangle_c_name(name)
                if name != unmangled_name:
                    yield Export(unmangled_name), AbsoluteVirtualAddress(sym.address)

    for sym in bv.get_symbols_of_type(SymbolType.DataSymbol):
        if sym.binding not in [SymbolBinding.GlobalBinding]:
            continue

        name = sym.short_name
        if not name.startswith("__forwarder_name"):
            continue

        # Due to https://github.com/Vector35/binaryninja-api/issues/4641, in binja version 3.5, the symbol's name
        # does not contain the DLL name. As a workaround, we read the C string at the symbol's address, which contains
        # both the DLL name and the function name.
        # Once the above issue is closed in the next binjs stable release, we can update the code here to use the
        # symbol name directly.
        name = read_c_string(bv, sym.address, 1024)
        forwarded_name = capa.features.extractors.helpers.reformat_forwarded_export_name(name)
        yield Export(forwarded_name), AbsoluteVirtualAddress(sym.address)
        yield Characteristic("forwarded export"), AbsoluteVirtualAddress(sym.address)


def extract_file_import_names(bv: BinaryView) -> Iterator[tuple[Feature, Address]]:
    """extract function imports

    1. imports by ordinal:
     - modulename.#ordinal

    2. imports by name, results in two features to support importname-only
       matching:
     - modulename.importname
     - importname
    """
    for sym in bv.get_symbols_of_type(SymbolType.ImportAddressSymbol):
        lib_name = str(sym.namespace)
        addr = AbsoluteVirtualAddress(sym.address)
        for name in capa.features.extractors.helpers.generate_symbols(lib_name, sym.short_name, include_dll=True):
            yield Import(name), addr

        ordinal = sym.ordinal
        if ordinal != 0 and (lib_name != ""):
            ordinal_name = f"#{ordinal}"
            for name in capa.features.extractors.helpers.generate_symbols(lib_name, ordinal_name, include_dll=True):
                yield Import(name), addr


def extract_file_section_names(bv: BinaryView) -> Iterator[tuple[Feature, Address]]:
    """extract section names"""
    for name, section in bv.sections.items():
        yield Section(name), AbsoluteVirtualAddress(section.start)


def extract_file_strings(bv: BinaryView) -> Iterator[tuple[Feature, Address]]:
    """extract ASCII and UTF-16 LE strings"""
    for s in bv.strings:
        yield String(s.value), FileOffsetAddress(s.start)


def extract_file_function_names(bv: BinaryView) -> Iterator[tuple[Feature, Address]]:
    """
    extract the names of statically-linked library functions.
    """
    for sym_name in bv.symbols:
        for sym in bv.symbols[sym_name]:
            if sym.type not in [SymbolType.LibraryFunctionSymbol, SymbolType.FunctionSymbol]:
                continue

            name = sym.short_name
            yield FunctionName(name), sym.address
            if name.startswith("_"):
                # some linkers may prefix linked routines with a `_` to avoid name collisions.
                # extract features for both the mangled and un-mangled representations.
                # e.g. `_fwrite` -> `fwrite`
                # see: https://stackoverflow.com/a/2628384/87207
                yield FunctionName(name[1:]), sym.address


def extract_file_format(bv: BinaryView) -> Iterator[tuple[Feature, Address]]:
    if bv.file.database is not None:
        yield Format(FORMAT_BINJA_DB), NO_ADDRESS

    view_type = bv.view_type
    if view_type in ["PE", "COFF"]:
        yield Format(FORMAT_PE), NO_ADDRESS
    elif view_type == "ELF":
        yield Format(FORMAT_ELF), NO_ADDRESS
    elif view_type == "Mapped":
        if bv.arch.name == "x86":
            yield Format(FORMAT_SC32), NO_ADDRESS
        elif bv.arch.name == "x86_64":
            yield Format(FORMAT_SC64), NO_ADDRESS
        else:
            raise NotImplementedError(f"unexpected raw file with arch: {bv.arch}")
    elif view_type == "Raw":
        # no file type to return when processing a binary file, but we want to continue processing
        return
    else:
        raise NotImplementedError(f"unexpected file format: {view_type}")


def extract_features(bv: BinaryView) -> Iterator[tuple[Feature, Address]]:
    """extract file features"""
    for file_handler in FILE_HANDLERS:
        for feature, addr in file_handler(bv):
            yield feature, addr


FILE_HANDLERS = (
    extract_file_export_names,
    extract_file_import_names,
    extract_file_strings,
    extract_file_section_names,
    extract_file_embedded_pe,
    extract_file_function_names,
    extract_file_format,
)
