from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from axiom.rag.models import CodeChunk, ImportBinding, SymbolDefinition, SymbolReference

IDENT = r"[A-Za-z_][A-Za-z0-9_]*"


@dataclass(slots=True)
class SymbolExtraction:
    definitions: list[SymbolDefinition]
    imports: list[ImportBinding]
    references: list[SymbolReference]


def extract_symbols(
    file_path: str,
    language: str,
    source: str,
    chunks: list[CodeChunk],
) -> SymbolExtraction:
    if language == "python":
        extracted = _extract_python(file_path, language, source, chunks)
    elif language == "java":
        extracted = _extract_java(file_path, language, source, chunks)
    elif language in {"javascript", "typescript"}:
        extracted = _extract_jsts(file_path, language, source, chunks)
    else:
        return SymbolExtraction([], [], [])
    # Repository-scale extraction can observe the same stable entity more than
    # once (for example repeated nested definitions or nested calls on one
    # source line). Collapse identical stable IDs before SQLite uniqueness
    # boundaries while preserving extraction order.
    extracted.definitions = list(
        {definition.id: definition for definition in extracted.definitions}.values()
    )
    extracted.imports = list({binding.id: binding for binding in extracted.imports}.values())
    extracted.references = list(
        {reference.id: reference for reference in extracted.references}.values()
    )
    return extracted


def stable_symbol_id(
    *,
    file_path: str,
    language: str,
    symbol_kind: str,
    qualified_name: str,
    signature: str | None,
) -> str:
    payload = "|".join(
        [
            language,
            Path(file_path).as_posix(),
            symbol_kind,
            qualified_name,
            _normalize_signature(signature or ""),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_reference_id(
    *,
    file_path: str,
    enclosing_symbol_id: str | None,
    reference_kind: str,
    name: str,
    qualifier: str | None,
    start_line: int,
    end_line: int,
) -> str:
    payload = "|".join(
        [
            Path(file_path).as_posix(),
            enclosing_symbol_id or "",
            reference_kind,
            qualifier or "",
            name,
            str(start_line),
            str(end_line),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _extract_python(
    file_path: str,
    language: str,
    source: str,
    chunks: list[CodeChunk],
) -> SymbolExtraction:
    definitions = _definitions_from_chunks(file_path, language, chunks)
    imports: list[ImportBinding] = []
    references: list[SymbolReference] = []
    line_defs = _line_to_enclosing(definitions)
    for line_number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        imports.extend(_python_imports(file_path, language, stripped, line_number))
        if _is_python_declaration_or_import(stripped):
            continue
        if "getattr(" in stripped or "globals(" in stripped or "eval(" in stripped:
            references.append(
                _reference(file_path, language, "dynamic", "dynamic", None, line_number, line_defs)
            )
            continue
        for qualifier, name, args in _call_matches(stripped):
            if name in {"if", "for", "while", "return", "class", "def"}:
                continue
            kind = "constructor_call" if name[:1].isupper() else "call"
            if qualifier in {"self", "this"}:
                kind = "member_call"
            references.append(
                _reference(file_path, language, kind, name, qualifier, line_number, line_defs, args)
            )
    return SymbolExtraction(definitions, imports, references)


def _extract_java(
    file_path: str,
    language: str,
    source: str,
    chunks: list[CodeChunk],
) -> SymbolExtraction:
    definitions = _definitions_from_chunks(file_path, language, chunks)
    imports: list[ImportBinding] = []
    references: list[SymbolReference] = []
    line_defs = _line_to_enclosing(definitions)
    package_name = ""
    for line_number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        package = re.match(r"package\s+([\w.]+)\s*;", stripped)
        if package:
            package_name = package.group(1)
            continue
        imported = re.match(r"import\s+(static\s+)?([\w.]+)(\.\*)?\s*;", stripped)
        if imported:
            module_name = imported.group(2)
            wildcard = bool(imported.group(3))
            imports.append(
                _import(
                    file_path,
                    language,
                    module_name,
                    "*" if wildcard else module_name.rsplit(".", 1)[-1],
                    "*" if wildcard else module_name.rsplit(".", 1)[-1],
                    "static_wildcard"
                    if wildcard and imported.group(1)
                    else "wildcard"
                    if wildcard
                    else "static"
                    if imported.group(1)
                    else "import",
                    line_number,
                    0,
                )
            )
            continue
        if _is_java_declaration(stripped):
            continue
        for qualifier, name, args in _call_matches(stripped):
            if name in {"if", "for", "while", "switch", "return", "new"}:
                continue
            references.append(
                _reference(
                    file_path,
                    language,
                    "member_call" if qualifier else "call",
                    name,
                    qualifier,
                    line_number,
                    line_defs,
                    args,
                )
            )
        for qualifier, name in _constructor_matches(stripped):
            references.append(
                _reference(
                    file_path,
                    language,
                    "constructor_call",
                    name,
                    qualifier or None,
                    line_number,
                    line_defs,
                )
            )
        if package_name:
            continue
    return SymbolExtraction(definitions, imports, references)


def _extract_jsts(
    file_path: str,
    language: str,
    source: str,
    chunks: list[CodeChunk],
) -> SymbolExtraction:
    definitions = _definitions_from_chunks(file_path, language, chunks)
    imports: list[ImportBinding] = []
    references: list[SymbolReference] = []
    line_defs = _line_to_enclosing(definitions)
    for line_number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        imports.extend(_jsts_imports(file_path, language, stripped, line_number))
        if _is_jsts_declaration_or_import(stripped):
            continue
        if "import(" in stripped or "require(variable" in stripped:
            references.append(
                _reference(file_path, language, "dynamic", "dynamic", None, line_number, line_defs)
            )
            continue
        for qualifier, name in _constructor_matches(stripped):
            references.append(
                _reference(
                    file_path,
                    language,
                    "constructor_call",
                    name,
                    qualifier or None,
                    line_number,
                    line_defs,
                )
            )
        for qualifier, name, args in _call_matches(stripped):
            if name in {"if", "for", "while", "switch", "return", "function"}:
                continue
            references.append(
                _reference(
                    file_path,
                    language,
                    "member_call" if qualifier else "call",
                    name,
                    qualifier,
                    line_number,
                    line_defs,
                    args,
                )
            )
    return SymbolExtraction(definitions, imports, references)


def _definitions_from_chunks(
    file_path: str,
    language: str,
    chunks: list[CodeChunk],
) -> list[SymbolDefinition]:
    definitions: list[SymbolDefinition] = []
    by_qualified: dict[str, SymbolDefinition] = {}
    for chunk in chunks:
        if chunk.chunk_type == "file" or not chunk.symbol_name or not chunk.qualified_name:
            continue
        signature = _signature(chunk)
        symbol_id = stable_symbol_id(
            file_path=file_path,
            language=language,
            symbol_kind=chunk.chunk_type,
            qualified_name=chunk.qualified_name,
            signature=signature,
        )
        parent_id = None
        if chunk.parent_symbol and "." in chunk.qualified_name:
            parent_qualified = chunk.qualified_name.rsplit(".", 1)[0]
            parent = by_qualified.get(parent_qualified)
            parent_id = parent.id if parent else None
        definition = SymbolDefinition(
            id=symbol_id,
            file_path=file_path,
            language=language,
            symbol_kind=chunk.chunk_type,
            name=chunk.symbol_name,
            qualified_name=chunk.qualified_name,
            container_symbol_id=parent_id,
            container_qualified_name=chunk.qualified_name.rsplit(".", 1)[0]
            if "." in chunk.qualified_name
            else None,
            signature=signature,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            chunk_id=chunk.id,
            exported=_is_exported(chunk),
            visibility=_visibility(chunk.content),
            definition_hash=hashlib.sha256(
                (signature or chunk.qualified_name).encode()
            ).hexdigest(),
        )
        definitions.append(definition)
        by_qualified[chunk.qualified_name] = definition
    return definitions


def _python_imports(
    file_path: str,
    language: str,
    stripped: str,
    line_number: int,
) -> list[ImportBinding]:
    result: list[ImportBinding] = []
    match = re.match(r"import\s+(.+)$", stripped)
    if match:
        for part in match.group(1).split(","):
            module, _, alias = part.strip().partition(" as ")
            local = alias.strip() or module.rsplit(".", 1)[-1]
            result.append(
                _import(
                    file_path,
                    language,
                    module.strip(),
                    None,
                    local,
                    "import",
                    line_number,
                    0,
                )
            )
    match = re.match(r"from\s+([\.\w]+)\s+import\s+(.+)$", stripped)
    if match:
        module = match.group(1)
        level = len(module) - len(module.lstrip("."))
        module_name = module.lstrip(".")
        for part in match.group(2).split(","):
            name, _, alias = part.strip().partition(" as ")
            kind = "wildcard" if name == "*" else "from_import"
            local = alias.strip() or name
            result.append(
                _import(
                    file_path,
                    language,
                    module_name,
                    name,
                    local,
                    kind,
                    line_number,
                    level,
                )
            )
    return result


def _jsts_imports(
    file_path: str,
    language: str,
    stripped: str,
    line_number: int,
) -> list[ImportBinding]:
    result: list[ImportBinding] = []
    module_match = re.search(r"from\s+['\"]([^'\"]+)['\"]", stripped)
    module_name = module_match.group(1) if module_match else ""
    if stripped.startswith("import ") and module_name:
        default = re.match(rf"import\s+({IDENT})\s+from", stripped)
        if default:
            result.append(
                _import(
                    file_path,
                    language,
                    module_name,
                    "default",
                    default.group(1),
                    "default",
                    line_number,
                    0,
                )
            )
        namespace = re.match(rf"import\s+\*\s+as\s+({IDENT})\s+from", stripped)
        if namespace:
            result.append(
                _import(
                    file_path,
                    language,
                    module_name,
                    "*",
                    namespace.group(1),
                    "namespace",
                    line_number,
                    0,
                )
            )
        named = re.search(r"\{([^}]+)\}", stripped)
        if named:
            for item in named.group(1).split(","):
                imported, _, alias = item.strip().partition(" as ")
                result.append(
                    _import(
                        file_path,
                        language,
                        module_name,
                        imported.strip(),
                        alias.strip() or imported.strip(),
                        "named",
                        line_number,
                        0,
                    )
                )
    require = re.match(rf"const\s+({IDENT})\s*=\s*require\(['\"]([^'\"]+)['\"]\)", stripped)
    if require:
        result.append(
            _import(
                file_path,
                language,
                require.group(2),
                "default",
                require.group(1),
                "require",
                line_number,
                0,
            )
        )
    return result


def _call_matches(stripped: str) -> list[tuple[str | None, str, int]]:
    result: list[tuple[str | None, str, int]] = []
    call_pattern = rf"(?:(?P<qual>{IDENT})\.)?(?P<name>{IDENT})\s*\((?P<args>[^)]*)\)"
    for match in re.finditer(call_pattern, stripped):
        if stripped[: match.start()].rstrip().endswith("new"):
            continue
        args = match.group("args").strip()
        count = 0 if not args else len([part for part in args.split(",") if part.strip()])
        result.append((match.group("qual"), match.group("name"), count))
    return result


def _constructor_matches(stripped: str) -> list[tuple[str, str]]:
    return re.findall(rf"new\s+(?:(?P<qual>{IDENT})\.)?(?P<name>{IDENT})\s*\(", stripped)


def _is_python_declaration_or_import(stripped: str) -> bool:
    return stripped.startswith(("def ", "async def ", "class ", "import ", "from "))


def _is_java_declaration(stripped: str) -> bool:
    if not stripped.endswith("{"):
        return False
    if re.match(r"(public|private|protected|static|final|\s)+\s*class\b", stripped):
        return True
    if re.match(r"(public|private|protected|static|final|\s)+\s*(interface|enum)\b", stripped):
        return True
    return bool(
        re.match(
            rf"(public|private|protected|static|final|\s)+[\w<>\[\], ?]+\s+{IDENT}\s*\(",
            stripped,
        )
    )


def _is_jsts_declaration_or_import(stripped: str) -> bool:
    if stripped.startswith("import "):
        return True
    if stripped.startswith(
        (
            "export function ",
            "function ",
            "export class ",
            "class ",
            "export interface ",
            "interface ",
            "export type ",
            "type ",
        )
    ):
        return True
    if "=>" in stripped and re.match(rf"(export\s+)?const\s+{IDENT}\s*=", stripped):
        return True
    return bool(stripped.endswith("{") and re.match(rf"{IDENT}\s*\(", stripped))


def _reference(
    file_path: str,
    language: str,
    kind: str,
    name: str,
    qualifier: str | None,
    line_number: int,
    line_defs: dict[int, SymbolDefinition],
    argument_count: int | None = None,
) -> SymbolReference:
    enclosing = line_defs.get(line_number)
    status = "dynamic" if kind == "dynamic" else "unresolved"
    return SymbolReference(
        id=stable_reference_id(
            file_path=file_path,
            enclosing_symbol_id=enclosing.id if enclosing else None,
            reference_kind=kind,
            name=name,
            qualifier=qualifier,
            start_line=line_number,
            end_line=line_number,
        ),
        file_path=file_path,
        language=language,
        reference_kind=kind,
        name=name,
        qualifier=qualifier,
        enclosing_symbol_id=enclosing.id if enclosing else None,
        enclosing_qualified_name=enclosing.qualified_name if enclosing else None,
        argument_count=argument_count,
        start_line=line_number,
        end_line=line_number,
        resolved_symbol_id=None,
        resolution_status=status,
        resolution_confidence=0.0,
        resolution_reason="dynamic reference" if kind == "dynamic" else None,
    )


def _import(
    file_path: str,
    language: str,
    module_name: str,
    imported_name: str | None,
    local_name: str | None,
    import_kind: str,
    line_number: int,
    relative_level: int,
) -> ImportBinding:
    status = "dynamic" if import_kind == "wildcard" else "unresolved"
    payload = "|".join(
        [file_path, language, module_name, imported_name or "", local_name or "", str(line_number)]
    )
    return ImportBinding(
        id=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        file_path=file_path,
        language=language,
        module_name=module_name,
        imported_name=imported_name,
        local_name=local_name,
        import_kind=import_kind,
        relative_level=relative_level,
        start_line=line_number,
        end_line=line_number,
        resolved_file_path=None,
        resolution_status=status,
    )


def _line_to_enclosing(definitions: list[SymbolDefinition]) -> dict[int, SymbolDefinition]:
    result: dict[int, SymbolDefinition] = {}
    ordered = sorted(definitions, key=lambda item: item.end_line - item.start_line)
    for definition in ordered:
        for line in range(definition.start_line, definition.end_line + 1):
            result[line] = definition
    return result


def _signature(chunk: CodeChunk) -> str:
    first = (
        chunk.content.splitlines()[0].strip()
        if chunk.content.splitlines()
        else chunk.symbol_name or ""
    )
    return _normalize_signature(first)


def _normalize_signature(signature: str) -> str:
    return re.sub(r"\s+", " ", signature.strip())


def _is_exported(chunk: CodeChunk) -> bool:
    if chunk.language in {"javascript", "typescript"}:
        return "export " in chunk.content[:80]
    if chunk.language == "python":
        return not (chunk.symbol_name or "").startswith("_")
    return "private " not in chunk.content[:80]


def _visibility(content: str) -> str | None:
    head = content[:120]
    for value in ["public", "private", "protected"]:
        if re.search(rf"\b{value}\b", head):
            return value
    return None
