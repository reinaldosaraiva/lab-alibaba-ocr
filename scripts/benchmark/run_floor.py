"""Benchmark floor (P001-S003): FPR/recall de SAST determinístico sobre o gold.

Implementa o método pré-registrado em plans/P001-S003-results.md (DR5, itens
1-22): reconstrói um arquivo por hunk do gold (conjuntos D e C), roda a
ferramenta escolhida (--tool semgrep|bandit|codeql) e mede FP rate, recall de
finding e recall de CWE por linguagem × slice (full/unseen), com intervalo de
Wilson 95% e o piso de FPR por linguagem (piso_det). Runs de ferramentas
distintas acumulam num único floor.json: cada run substitui apenas a própria
seção (merge por --tool), desde que apontem para o MESMO gold (gold.sha256;
drift de gold é erro, não merge silencioso).
"""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TOOL_CHOICES = ("semgrep", "bandit", "codeql")
GROUPS = ("py", "c", "tsjs", "go")
LANGUAGE_TO_GROUP = {
    "python": "py",
    "c": "c",
    "typescript": "tsjs",
    "javascript": "tsjs",
    "go": "go",
}
SEMGREP_CONFIG = {
    "py": ["p/python"],
    "c": ["p/c"],
    "tsjs": ["p/typescript", "p/javascript"],
    "go": ["p/golang"],
}
CODEQL_LANGUAGE = {"py": "python", "tsjs": "javascript"}
CODEQL_SUITE_ENV = {"py": "CODEQL_PY_SUITE", "tsjs": "CODEQL_JS_SUITE"}
Z95 = 1.959964
SEMGREP_TIMEOUT = 1800
BANDIT_TIMEOUT = 600
CODEQL_PACK_TIMEOUT = 300
CODEQL_CREATE_TIMEOUT = 1800
CODEQL_ANALYZE_TIMEOUT = 900
CHUNK_SIZE = 1 << 20
ID_PATTERN = re.compile(r"[0-9a-f]{16}(-[a-z0-9]+)?")
CWE_PATTERN = re.compile(r"CWE-\d+")
# codeql SARIF usa o tag "external/cwe/cwe-787"; DR5 item 11 manda best-effort
CWE_TAG_PATTERN = re.compile(r"external/cwe/cwe-(\d+)", re.IGNORECASE)
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
NOTE_EXCERPT_LIMIT = 400


@dataclass
class Finding:
    path: str
    rule: str
    start: int
    end: int
    cwes: set[str] = field(default_factory=set)


@dataclass
class ToolRun:
    status: dict[str, str] = field(default_factory=dict)
    reason: dict[str, str | None] = field(default_factory=dict)
    findings: dict[str, dict[str, list[Finding]]] = field(default_factory=dict)
    parse_failures: dict[str, int] = field(default_factory=dict)


def _fail(message: str) -> None:
    print(f"run_floor: {message}", file=sys.stderr)
    raise SystemExit(1)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for number, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError as exc:
                    _fail(f"gold linha {number} não é JSON ({path}): {exc}")
                if not isinstance(payload, dict):
                    _fail(f"gold linha {number} não é objeto ({path})")
                records.append(payload)
    except OSError as exc:
        _fail(f"gold ilegível ({path}): {exc}")
    return records


def _unseen_ids(path: Path) -> list[str]:
    """Aceita tanto registros completos quanto linhas com o id puro."""
    ids: list[str] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    payload: object = json.loads(line)
                except ValueError:
                    payload = line
                if isinstance(payload, dict) and payload.get("id"):
                    ids.append(str(payload["id"]))
                elif isinstance(payload, str) and payload:
                    ids.append(payload)
    except OSError as exc:
        _fail(f"unseen ilegível ({path}): {exc}")
    return list(dict.fromkeys(ids))


def _findings_of(record: dict) -> list[dict]:
    label = record.get("label")
    findings = label.get("findings") if isinstance(label, dict) else None
    if not isinstance(findings, list):
        return []
    return [item for item in findings if isinstance(item, dict)]


def _group_of(record: dict) -> str | None:
    return LANGUAGE_TO_GROUP.get(str(record.get("language", "")))


def _annotated_lines(record: dict) -> list[int]:
    lines: list[int] = []
    for finding in _findings_of(record):
        raw = finding.get("line")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        lines.append(int(raw))
    return lines


def _annotated_cwes(record: dict) -> set[str]:
    return {
        str(finding["cwe"])
        for finding in _findings_of(record)
        if isinstance(finding.get("cwe"), str) and finding["cwe"]
    }


def _first_hunk(diff: str) -> tuple[int, int, list[str]] | None:
    header: tuple[int, int] | None = None
    lines: list[str] = []
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            if in_hunk:
                break
            match = HUNK_HEADER.match(line)
            if match is None:
                continue
            new_len = int(match.group(2)) if match.group(2) else 1
            header = (int(match.group(1)), new_len)
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith(("+", " ")):
            lines.append(line[1:])
    if header is None:
        return None
    return header[0], header[1], lines


def _join(lines: list[str]) -> str:
    return "\n".join(lines) + "\n" if lines else ""


def _reconstruct(record: dict) -> tuple[str, str, int] | None:
    """(basename, conteúdo, total de linhas) ou None se não reconstruível.

    Sempre do PRIMEIRO hunk do `diff` (hunk PÓS-mutação, contém o defeito);
    `reference_diff` é a referência PRÉ-mutação (proveniência) e não é alvo
    de scan.
    """
    name = Path(str(record.get("file") or "")).name or "file.txt"
    diff = record.get("diff")
    if not isinstance(diff, str) or not diff.strip():
        return None
    hunk = _first_hunk(diff)
    if hunk is None:
        return None
    new_start, _, lines = hunk
    start = record.get("new_start") or new_start or 1
    try:
        start = max(int(start), 1)
    except (TypeError, ValueError):
        start = 1
    # placeholders vazios posicionam o primeiro hunk no new_start absoluto (item 7)
    padded = [""] * (start - 1) + lines
    return name, _join(padded), len(padded)


def _valid_record_id(record_id: str) -> bool:
    """Defense-in-depth: o id do gold vira path component em work/<grupo>/<id>.

    Os ids reais do gold casam com ID_PATTERN; um id malformado (ex. com
    "../") não pode escapar do diretório do grupo nem virar arquivo.
    """
    return ID_PATTERN.fullmatch(record_id) is not None


def _rebuild(
    work: Path, corpus: list[dict]
) -> tuple[dict[str, list[dict]], dict[str, int], list[str]]:
    by_group: dict[str, list[dict]] = {group: [] for group in GROUPS}
    for record in corpus:
        group = _group_of(record)
        if group is not None:
            by_group[group].append(record)
    for group in GROUPS:
        group_dir = work / group
        shutil.rmtree(group_dir, ignore_errors=True)  # idempotente (item 21)
        group_dir.mkdir(parents=True, exist_ok=True)
    line_counts: dict[str, int] = {}
    not_built: list[str] = []
    for group in GROUPS:
        for record in by_group[group]:
            record_id = str(record.get("id"))
            if not _valid_record_id(record_id):
                # id malformado não vira path component (defense-in-depth)
                not_built.append(record_id)
                continue
            built = _reconstruct(record)
            if built is None:
                not_built.append(record_id)
                continue
            name, content, total = built
            target = work / group / record_id / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            line_counts[record_id] = total
    return by_group, line_counts, not_built


def _record_unscorable(record: dict, line_counts: dict[str, int]) -> bool:
    lines = _annotated_lines(record)
    if not lines:
        return True
    total = line_counts.get(str(record.get("id")))
    if total is None:
        return True
    return any(line < 1 or line > total for line in lines)


def _unscorable_ids(corpus: list[dict], line_counts: dict[str, int]) -> list[str]:
    """Ids do corpus não pontuáveis nesta run (ordenado).

    Entra: registro COM grupo de linguagem sem reconstrução (defect OU clean
    — sem arquivo não há detecção nem falso positivo a mensurar) e defect
    cujas linhas anotadas caem fora do range reconstruído. Registros SEM
    grupo (`_group_of` None) não entram: já saem em excluded_languages e
    nunca são pontuados. scoreable = corpus - unscorable.
    """
    ids: set[str] = set()
    for record in corpus:
        record_id = str(record.get("id"))
        if _group_of(record) is None:
            continue
        if record_id not in line_counts or (
            _findings_of(record) and _record_unscorable(record, line_counts)
        ):
            ids.add(record_id)
    return sorted(ids)


def _record_id(path: str) -> str | None:
    for part in re.split(r"[\\/]+", path):
        if ID_PATTERN.fullmatch(part):
            return part
    return None


def _attribute(findings: list[Finding]) -> tuple[dict[str, list[Finding]], int]:
    by_id: dict[str, list[Finding]] = {}
    dropped = 0
    for finding in findings:
        record_id = _record_id(finding.path)
        if record_id is None:
            dropped += 1
            continue
        by_id.setdefault(record_id, []).append(finding)
    return by_id, dropped


def _cwes_from_strings(values: list[object]) -> set[str]:
    cwes: set[str] = set()
    for value in values:
        if isinstance(value, str):
            cwes.update(CWE_PATTERN.findall(value))
    return cwes


def _cwes_from_metadata(metadata: dict) -> set[str]:
    raw = metadata.get("cwe")
    values = raw if isinstance(raw, list) else [raw]
    return _cwes_from_strings(values)


def _cwes_from_tags(tags: object) -> set[str]:
    cwes: set[str] = set()
    if not isinstance(tags, list):
        return cwes
    for tag in tags:
        if not isinstance(tag, str):
            continue
        if CWE_PATTERN.fullmatch(tag):
            cwes.add(tag)
        else:
            match = CWE_TAG_PATTERN.fullmatch(tag)
            if match:
                cwes.add(f"CWE-{match.group(1)}")
    return cwes


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _run_tool(
    command: list[str], timeout: int, what: str, notes: list[str]
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        notes.append(f"{what}: timeout após {timeout}s")
    except OSError as exc:
        notes.append(f"{what}: não executou ({exc})")
    return None


def _note_failure(
    notes: list[str], what: str, proc: subprocess.CompletedProcess[str] | None
) -> None:
    if proc is None:
        return  # _run_tool já registrou a causa
    excerpt = (proc.stderr or "").strip().replace("\n", " ")
    notes.append(f"{what}: exit {proc.returncode}: {excerpt[:NOTE_EXCERPT_LIMIT]}")


def _parse_stdout_json(
    proc: subprocess.CompletedProcess[str], what: str, notes: list[str]
) -> dict | None:
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        notes.append(f"{what}: stdout não é JSON")
        return None
    return payload if isinstance(payload, dict) else None


def _semgrep_metadata(result: dict) -> dict:
    # semgrep aninha metadata em extra; aceita também no topo (defensivo)
    if isinstance(result.get("metadata"), dict):
        return result["metadata"]
    extra = result.get("extra")
    if isinstance(extra, dict) and isinstance(extra.get("metadata"), dict):
        return extra["metadata"]
    return {}


def _parse_semgrep(payload: dict) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    for result in payload.get("results", []):
        if not isinstance(result, dict):
            continue
        start = result.get("start")
        end = result.get("end")
        findings.append(
            Finding(
                path=str(result.get("path", "")),
                rule=str(result.get("check_id", "")),
                start=_as_int(start.get("line") if isinstance(start, dict) else None),
                end=_as_int(end.get("line") if isinstance(end, dict) else None),
                cwes=_cwes_from_metadata(_semgrep_metadata(result)),
            )
        )
    errors = payload.get("errors")
    return findings, len(errors) if isinstance(errors, list) else 0


def _parse_bandit(payload: dict) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    for issue in payload.get("results", []):
        if not isinstance(issue, dict):
            continue
        issue_cwe = issue.get("issue_cwe")
        cwe_id = issue_cwe.get("id") if isinstance(issue_cwe, dict) else None
        start = _as_int(issue.get("line_number"))
        findings.append(
            Finding(
                path=str(issue.get("filename", "")),
                rule=str(issue.get("test_id", "")),
                start=start,
                end=_as_int(issue.get("end_line_number"), default=start),
                cwes={f"CWE-{cwe_id}"} if isinstance(cwe_id, int) else set(),
            )
        )
    errors = payload.get("errors")
    return findings, len(errors) if isinstance(errors, list) else 0


def _parse_sarif(text: str) -> tuple[list[Finding], int]:
    """(findings, nº de syntax-error) — erros de parse do extractor, não achados."""
    findings: list[Finding] = []
    syntax_errors = 0
    payload = json.loads(text)
    runs = payload.get("runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list) or not runs or not isinstance(runs[0], dict):
        return findings, 0
    run = runs[0]
    tool = run.get("tool")
    driver = tool.get("driver") if isinstance(tool, dict) else None
    rules_raw = driver.get("rules") if isinstance(driver, dict) else None
    rules = (
        [rule for rule in rules_raw if isinstance(rule, dict)]
        if isinstance(rules_raw, list)
        else []
    )
    cwes_by_rule: dict[str, set[str]] = {}
    for index, rule in enumerate(rules):
        props = rule.get("properties")
        props = props if isinstance(props, dict) else {}
        cwes = _cwes_from_metadata(props) | _cwes_from_tags(props.get("tags"))
        rule_id = str(rule.get("id") or f"rule-{index}")
        cwes_by_rule[rule_id] = cwes
        cwes_by_rule[f"#{index}"] = cwes
    results = run.get("results")
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict):
            continue
        rule_id = result.get("ruleId")
        if not isinstance(rule_id, str) or not rule_id:
            rule_ref = result.get("rule")
            index = rule_ref.get("index") if isinstance(rule_ref, dict) else None
            rule_id = f"#{index}" if isinstance(index, int) else ""
        if rule_id.endswith("syntax-error"):
            # ex. js/syntax-error, py/syntax-error: falha de parse do extractor
            syntax_errors += 1
            continue
        locations = result.get("locations")
        location: dict = {}
        if isinstance(locations, list) and locations:
            first = locations[0]
            location = first if isinstance(first, dict) else {}
        physical = location.get("physicalLocation")
        physical = physical if isinstance(physical, dict) else {}
        region = physical.get("region")
        region = region if isinstance(region, dict) else {}
        start = _as_int(region.get("startLine"))
        artifact = physical.get("artifactLocation")
        uri = artifact.get("uri") if isinstance(artifact, dict) else ""
        if isinstance(uri, str) and uri.startswith("file://"):
            uri = uri[len("file://") :]
        findings.append(
            Finding(
                path=str(uri),
                rule=str(rule_id),
                start=start,
                end=_as_int(region.get("endLine"), default=start),
                cwes=cwes_by_rule.get(str(rule_id), set()),
            )
        )
    return findings, syntax_errors


def _run_semgrep(work: Path, built: dict[str, int], notes: list[str]) -> ToolRun:
    run = ToolRun()
    for group in GROUPS:
        run.findings[group] = {}
        if not built.get(group):
            run.status[group] = "ok"  # sem arquivos: célula zerada, não falha
            continue
        what = f"semgrep {group}"
        # --no-git-ignore: o workdir fica sob .qwen/ (gitignored) e o semgrep
        # 1.145.0 respeita .gitignore por default (scan de 0 arquivos sem flag)
        command = [
            "semgrep",
            "scan",
            "--json",
            "--quiet",
            "--metrics=off",
            "--no-git-ignore",
            "--disable-version-check",
        ]
        # forma composta "a,b" falha com exit 7: uma flag --config por ruleset
        for ruleset in SEMGREP_CONFIG[group]:
            command += ["--config", ruleset]
        command.append(str(work / group))
        proc = _run_tool(command, SEMGREP_TIMEOUT, what, notes)
        if proc is None or proc.returncode != 0:
            _note_failure(notes, what, proc)
            run.status[group] = "failed"
            continue
        payload = _parse_stdout_json(proc, what, notes)
        if payload is None:
            run.status[group] = "failed"
            continue
        findings, errors = _parse_semgrep(payload)
        run.status[group] = "ok"
        run.parse_failures[group] = errors
        run.findings[group], dropped = _attribute(findings)
        if dropped:
            notes.append(f"{what}: {dropped} findings sem id de registro (descartados)")
    return run


def _run_bandit(work: Path, built: dict[str, int], notes: list[str]) -> ToolRun:
    run = ToolRun()
    for group in GROUPS:
        if group != "py":
            run.status[group] = "n/a"
            run.reason[group] = "bandit só aplica a python"
            continue
        if not built.get("py"):
            run.status["py"] = "ok"
            run.findings["py"] = {}
            continue
        what = "bandit py"
        command = ["bandit", "-r", str(work / "py"), "-f", "json", "-q"]
        proc = _run_tool(command, BANDIT_TIMEOUT, what, notes)
        if proc is None:
            run.status["py"] = "failed"
            continue
        payload = _parse_stdout_json(proc, what, notes)
        # bandit devolve exit 1 quando há issues: não é falha (DR5 item 18)
        if proc.returncode not in (0, 1) or payload is None or "results" not in payload:
            if proc.returncode not in (0, 1):
                notes.append(f"{what}: exit {proc.returncode} inesperado")
            run.status["py"] = "failed"
            continue
        findings, errors = _parse_bandit(payload)
        run.status["py"] = "ok"
        run.parse_failures["py"] = errors
        run.findings["py"], dropped = _attribute(findings)
        if dropped:
            notes.append(f"{what}: {dropped} findings sem id de registro (descartados)")
    return run


def _codeql_pack_suite(language: str) -> tuple[Path, str] | None:
    """(suíte, versão do pack) da maior versão instalada no cache de packs."""
    pattern = (
        f"codeql/{language}-queries/*/codeql-suites/{language}-security-and-quality.qls"
    )
    candidates = sorted(Path.home().joinpath(".codeql", "packages").glob(pattern))
    if not candidates:
        return None
    suite = candidates[-1]  # ordem lexicográfica reversa basta p/ versões
    return suite, suite.parent.parent.name


def _codeql_suite(group: str, notes: list[str]) -> tuple[Path | None, str | None]:
    """(suíte, versão do pack) ou (None, None) — item 19.

    O cask do brew NÃO inclui query packs: os packs oficiais vivem no cache
    ~/.codeql/packages (suítes `.qls` em codeql-suites/), baixáveis via
    `codeql pack download`.
    """
    language = CODEQL_LANGUAGE[group]
    override = os.environ.get(CODEQL_SUITE_ENV[group])
    if override:
        candidate = Path(override)
        if candidate.is_file():
            # versão do pack só é lida do glob do cache (Q3)
            return candidate, None
        notes.append(f"codeql: {CODEQL_SUITE_ENV[group]} aponta para arquivo ausente")
    found = _codeql_pack_suite(language)
    if found is not None:
        return found
    what = f"codeql pack download {language}"
    notes.append(f"codeql: suíte de {language} ausente no cache; baixando pack")
    proc = _run_tool(
        ["codeql", "pack", "download", f"codeql/{language}-queries"],
        CODEQL_PACK_TIMEOUT,
        what,
        notes,
    )
    if proc is not None and proc.returncode != 0:
        _note_failure(notes, what, proc)
    retry = _codeql_pack_suite(language)
    if retry is not None:
        return retry
    return None, None


def _run_codeql(
    work: Path, built: dict[str, int], notes: list[str]
) -> tuple[ToolRun, dict[str, str | None]]:
    run = ToolRun()
    # Q3: versão do query pack por linguagem (None se suíte não localizada/roda)
    packs: dict[str, str | None] = dict.fromkeys(CODEQL_LANGUAGE.values(), None)
    for group in GROUPS:
        if group not in CODEQL_LANGUAGE:
            run.status[group] = "n/a"
            if group == "c":
                run.reason[group] = (
                    "codeql C requer build; reconstruções não compilam "
                    "(registrado por §15 item 6)"
                )
            else:
                run.reason[group] = (
                    "sem arquivos"
                    if not built.get(group)
                    else "codeql go fora do escopo DR5"
                )
            continue
        if not built.get(group):
            run.status[group] = "ok"
            run.findings[group] = {}
            continue
        what = f"codeql {group}"
        language = CODEQL_LANGUAGE[group]
        suite, pack_version = _codeql_suite(group, notes)
        packs[language] = pack_version
        if suite is None:
            notes.append(
                f"{what}: suíte {language}-security-and-quality.qls não encontrada"
            )
            run.status[group] = "failed"
            continue
        database = work / f"db-{group}"
        shutil.rmtree(database, ignore_errors=True)  # create falha em db existente
        create = [
            "codeql",
            "database",
            "create",
            str(database),
            f"--language={CODEQL_LANGUAGE[group]}",
            f"--source-root={work / group}",
        ]
        proc = _run_tool(create, CODEQL_CREATE_TIMEOUT, f"{what} create", notes)
        if proc is None or proc.returncode != 0:
            _note_failure(notes, f"{what} create", proc)
            shutil.rmtree(database, ignore_errors=True)
            run.status[group] = "failed"
            continue
        sarif = work / f"{group}.sarif"
        analyze = [
            "codeql",
            "database",
            "analyze",
            str(database),
            str(suite),
            "--format=sarif-latest",
            f"--output={sarif}",
            "--threads=4",
        ]
        proc = _run_tool(analyze, CODEQL_ANALYZE_TIMEOUT, f"{what} analyze", notes)
        shutil.rmtree(database, ignore_errors=True)  # disco do host tem ~6GB livres
        if proc is None or proc.returncode != 0:
            _note_failure(notes, f"{what} analyze", proc)
            run.status[group] = "failed"
            continue
        try:
            findings, syntax_errors = _parse_sarif(sarif.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            notes.append(f"{what}: SARIF ilegível ({exc})")
            run.status[group] = "failed"
            continue
        run.status[group] = "ok"
        # syntax-error = falha de parse do extractor, não achado (item 19)
        run.parse_failures[group] = syntax_errors
        run.findings[group], dropped = _attribute(findings)
        if dropped:
            notes.append(f"{what}: {dropped} findings sem id de registro (descartados)")
    return run, packs


def _new_range(record: dict) -> tuple[int, int]:
    """Range novo do primeiro hunk do `diff` (DR5 item 12)."""
    diff = record.get("diff")
    hunk = _first_hunk(diff) if isinstance(diff, str) else None
    start = record.get("new_start") or (hunk[0] if hunk else None) or 1
    try:
        start = max(int(start), 1)
    except (TypeError, ValueError):
        start = 1
    length = hunk[1] if hunk else 1
    return start, start + length - 1


def _covers(finding: Finding, lines: list[int]) -> bool:
    return any(finding.start <= line <= finding.end for line in lines)


def _rate(part: int, total: int) -> float | None:
    return round(part / total, 6) if total else None


def _wilson(rate: float | None, n: int) -> list[float] | None:
    if rate is None or n <= 0:
        return None
    denominator = 1 + Z95**2 / n
    center = (rate + Z95**2 / (2 * n)) / denominator
    spread = math.sqrt(rate * (1 - rate) / n + Z95**2 / (4 * n**2))
    half = Z95 * spread / denominator
    # rate=0: o limite inferior exato é 0, mas o arredondamento float pode
    # produzir -0.0 (ou resíduo negativo) e vazar assim no floor.json
    return [max(0.0, round(center - half, 6)), round(center + half, 6)]


def _score(records: list[dict], findings_by_id: dict[str, list[Finding]]) -> dict:
    n_clean = n_defect = fp = detected = n_defect_cwe = cwe_detected = 0
    for record in records:
        record_id = str(record.get("id"))
        tool_findings = findings_by_id.get(record_id, [])
        if _findings_of(record):
            n_defect += 1
            lines = _annotated_lines(record)
            matched = [f for f in tool_findings if _covers(f, lines)]
            if matched:
                detected += 1
            annotated_cwes = _annotated_cwes(record)
            if annotated_cwes:
                n_defect_cwe += 1
                if any(f.cwes & annotated_cwes for f in matched):
                    cwe_detected += 1
        else:
            n_clean += 1
            new_start, new_end = _new_range(record)
            # findings em placeholder (start < new_start) são ignorados (item 12)
            if any(new_start <= f.start <= new_end for f in tool_findings):
                fp += 1
    fp_rate = _rate(fp, n_clean)
    recall = _rate(detected, n_defect)
    cwe_recall = _rate(cwe_detected, n_defect_cwe)
    return {
        "n_clean": n_clean,
        "n_defect": n_defect,
        "fp": fp,
        "fp_rate": fp_rate,
        "fp_wilson95": _wilson(fp_rate, n_clean),
        "detected": detected,
        "recall": recall,
        "recall_wilson95": _wilson(recall, n_defect),
        "n_defect_cwe": n_defect_cwe,
        "cwe_detected": cwe_detected,
        "cwe_recall": cwe_recall,
        "cwe_wilson95": _wilson(cwe_recall, n_defect_cwe),
    }


def _cell(
    group: str,
    ids: set[str],
    corpus_by_id: dict[str, dict],
    tool_run: ToolRun,
) -> dict:
    status = tool_run.status.get(group, "ok")
    if status == "n/a":
        return {"status": "n/a", "reason": tool_run.reason.get(group)}
    group_records = [
        corpus_by_id[record_id]
        for record_id in sorted(ids)
        if _group_of(corpus_by_id[record_id]) == group
    ]
    cell = _score(group_records, tool_run.findings.get(group, {}))
    if status == "failed":
        for key in (
            "fp",
            "fp_rate",
            "fp_wilson95",
            "detected",
            "recall",
            "recall_wilson95",
            "cwe_detected",
            "cwe_recall",
            "cwe_wilson95",
        ):
            cell[key] = None
        cell["parse_failures"] = None
    else:
        cell["parse_failures"] = tool_run.parse_failures.get(group, 0)
    cell["status"] = status
    return cell


def _piso_det(slices: dict) -> dict[str, float | None]:
    """Menor fp_rate no slice full entre ferramentas com recall > 0 (item 16)."""
    piso: dict[str, float | None] = {}
    full = slices.get("full", {})
    for group in GROUPS:
        candidates: list[float] = []
        for tool in TOOL_CHOICES:
            tools_block = full.get(tool)
            cell = tools_block.get(group) if isinstance(tools_block, dict) else None
            if not isinstance(cell, dict) or cell.get("status") != "ok":
                continue
            recall = cell.get("recall")
            fp_rate = cell.get("fp_rate")
            if (
                isinstance(recall, (int, float))
                and recall > 0
                and isinstance(fp_rate, (int, float))
            ):
                candidates.append(float(fp_rate))
        piso[group] = min(candidates) if candidates else None
    return piso


def _merge_slices(
    out_path: Path,
    tool: str,
    tool_cells: dict[str, dict[str, dict]],
    notes: list[str],
    unscorable: list[str],
    gold_sha: str,
) -> tuple[dict, dict, list[str], list[str]]:
    """floor.json acumula runs: só a seção do --tool desta run é substituída.

    Guard de drift de gold: o floor existente só é mesclado quando aponta
    para o mesmo gold (mesmo gold.sha256); proveniências diferentes não se
    misturam — o usuário precisa mover o floor antigo antes de reusar a
    saída (_fail).

    Retorna (slices mesclados, floor existente, notes, unscorable): notes e
    unscorable preservam as runs anteriores (união; notas deduplicadas
    mantendo a ordem — anteriores primeiro, atuais depois) e o existente
    preserva campos top-level de outras runs (ex. codeql_packs).
    """
    existing: dict = {}
    if out_path.exists():
        try:
            loaded = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            loaded = None
            notes.append(f"floor.json ilegível ({exc}); seções anteriores descartadas")
        if isinstance(loaded, dict):
            existing = loaded
    gold_block = existing.get("gold")
    previous_sha = gold_block.get("sha256") if isinstance(gold_block, dict) else None
    if previous_sha is not None and previous_sha != gold_sha:
        _fail(
            f"floor.json existente ({out_path}) aponta para outro gold "
            f"(sha256 {previous_sha} != {gold_sha}); mova o floor antigo "
            "antes de acumular runs nesta saída"
        )
    slices = existing.get("slices") if isinstance(existing.get("slices"), dict) else {}
    for name, cells in tool_cells.items():
        block = slices.get(name) if isinstance(slices.get(name), dict) else {}
        block[tool] = cells
        slices[name] = block
    previous_notes = existing.get("notes")
    if not isinstance(previous_notes, list):
        previous_notes = []
    merged_notes = list(dict.fromkeys([*previous_notes, *notes]))
    previous_unscorable = existing.get("unscorable")
    if not isinstance(previous_unscorable, list):
        previous_unscorable = []
    merged_unscorable = sorted(
        {str(value) for value in previous_unscorable} | set(unscorable)
    )
    return slices, existing, merged_notes, merged_unscorable


def _manifest_block(path: Path, notes: list[str]) -> dict:
    payload: object = None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        notes.append(f"lab_manifest ilegível ({path}): {exc}")
    data = payload if isinstance(payload, dict) else {}
    tools = data.get("tools") if isinstance(data.get("tools"), dict) else {}
    versions: dict[str, str | None] = {}
    for tool in TOOL_CHOICES:
        entry = tools.get(tool)
        versions[tool] = entry.get("actual") if isinstance(entry, dict) else None
    return {"generated_at": data.get("generated_at"), "tools": versions}


def _fmt(value: object) -> str:
    return f"{value:.3f}" if isinstance(value, (int, float)) else "null"


def _print_summary(tool: str, floor: dict, out_path: Path, rebuilt: int) -> None:
    sets = floor["sets"]
    print(
        f"run_floor: tool={tool} gold n={floor['gold']['n']} "
        f"(D={sets['defect']} C={sets['clean']} E={sets['excluded']}) "
        f"unseen n={floor['unseen']['n']} reconstruídos={rebuilt}"
    )
    # rótulos distintos para os dois "excluídos" do relatório:
    # sets.excluded = defect-manual SEM findings (nunca escaneado);
    # excluded_languages = registros fora das linguagens do escopo
    excluded_languages = floor.get("excluded_languages")
    if isinstance(excluded_languages, dict) and excluded_languages:
        langs = ", ".join(
            f"{language} {count}" for language, count in excluded_languages.items()
        )
    else:
        langs = "0"
    print(f"excluídos sem findings (defect-manual): {sets['excluded']}")
    print(f"fora do escopo de linguagem: {langs}")
    for name in ("full", "unseen"):
        cells = floor["slices"].get(name, {}).get(tool)
        if cells is None:
            continue
        print(f"slice {name}:")
        header = (
            f"  {'lang':5}  {'n_clean':>7}  {'n_defect':>8}  "
            f"{'fp_rate':>8}  {'recall':>7}  {'cwe':>7}  status"
        )
        print(header)
        for group in GROUPS:
            cell = cells.get(group, {})
            if cell.get("status") == "n/a":
                values = ("-", "-", "-", "-", "-")
            else:
                values = (
                    str(cell.get("n_clean", 0)),
                    str(cell.get("n_defect", 0)),
                    _fmt(cell.get("fp_rate")),
                    _fmt(cell.get("recall")),
                    _fmt(cell.get("cwe_recall")),
                )
            print(
                f"  {group:5}  {values[0]:>7}  {values[1]:>8}  {values[2]:>8}"
                f"  {values[3]:>7}  {values[4]:>7}  {cell.get('status', 'ok')}"
            )
    piso = floor["piso_det"]
    print("piso_det (full): " + "  ".join(f"{g}={_fmt(piso[g])}" for g in GROUPS))
    print(f"floor: {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mede o piso de FPR de uma ferramenta SAST sobre o gold (DR5).",
    )
    parser.add_argument("--tool", required=True, help="semgrep | bandit | codeql")
    parser.add_argument("--gold", required=True, help="JSONL do gold (somente leitura)")
    parser.add_argument(
        "--out", required=True, help="floor.json de saída (merge por --tool)"
    )
    parser.add_argument("--unseen", help="JSONL/ids do slice unseen (opcional)")
    parser.add_argument("--work", default=".qwen/tmp/bench-work")
    parser.add_argument("--manifest", default="results/lab-manifest.json")
    args = parser.parse_args()

    if args.tool not in TOOL_CHOICES:
        _fail(f"--tool inválido: {args.tool!r} (use semgrep, bandit ou codeql)")

    notes: list[str] = []
    gold_path = Path(args.gold)
    out_path = Path(args.out)
    work = Path(args.work)

    records = _load_jsonl(gold_path)
    gold_sha = _sha256(gold_path)
    unseen_ids = _unseen_ids(Path(args.unseen)) if args.unseen else []

    defect = [record for record in records if _findings_of(record)]
    defect_ids = {str(record.get("id")) for record in defect}
    overlap = sum(
        1
        for record in records
        if record.get("block") == "clean" and str(record.get("id")) in defect_ids
    )
    if overlap:
        notes.append(
            f"drift: {overlap} registros block=clean com findings ficam só em D"
        )
    clean = [
        record
        for record in records
        if record.get("block") == "clean" and str(record.get("id")) not in defect_ids
    ]
    # sets.excluded: defect-manual SEM findings (nem D nem C) — nunca
    # escaneados; distinto de excluded_languages (abaixo), que conta registros
    # fora das linguagens do escopo
    excluded = len(records) - len(defect) - len(clean)
    corpus = defect + clean
    corpus_by_id = {str(record.get("id")): record for record in corpus}

    # fora do escopo de linguagem (sem grupo em LANGUAGE_TO_GROUP, ex. shell,
    # rust): nunca reconstruídos/escaneados/pontuados — distinto de
    # sets.excluded (defect-manual sem findings)
    excluded_languages: dict[str, int] = {}
    for record in records:
        language = str(record.get("language", ""))
        if language not in LANGUAGE_TO_GROUP:
            excluded_languages[language] = excluded_languages.get(language, 0) + 1
    excluded_languages = dict(sorted(excluded_languages.items()))

    unseen_set = set(unseen_ids)
    unseen_drift = sorted(uid for uid in unseen_set if uid not in corpus_by_id)
    if unseen_drift:
        notes.append(f"unseen: {len(unseen_drift)} ids fora de D/C ignorados")

    work.mkdir(parents=True, exist_ok=True)
    by_group, line_counts, not_built = _rebuild(work, corpus)
    built_counts = {
        group: sum(1 for r in by_group[group] if str(r.get("id")) in line_counts)
        for group in GROUPS
    }
    if not_built:
        preview = ", ".join(sorted(not_built)[:5])
        notes.append(f"reconstrução falhou em {len(not_built)} registros ({preview})")

    unscorable = _unscorable_ids(corpus, line_counts)
    if unscorable:
        notes.append(
            f"unscorable: {len(unscorable)} registros não pontuáveis "
            "(sem reconstrução ou linhas de D fora do range)"
        )
    scoreable = set(corpus_by_id) - set(unscorable)

    codeql_packs: dict[str, str | None] | None = None
    if args.tool == "semgrep":
        tool_run = _run_semgrep(work, built_counts, notes)
    elif args.tool == "bandit":
        tool_run = _run_bandit(work, built_counts, notes)
    else:
        tool_run, codeql_packs = _run_codeql(work, built_counts, notes)

    slice_ids: dict[str, set[str]] = {"full": scoreable}
    if args.unseen is not None:
        slice_ids["unseen"] = scoreable & unseen_set
    tool_cells = {
        name: {group: _cell(group, ids, corpus_by_id, tool_run) for group in GROUPS}
        for name, ids in slice_ids.items()
    }

    # manifest ANTES do merge: _merge_slices fotografa notes/unscorable da
    # run corrente; notas appended depois seriam descartadas ao regravar
    manifest_block = _manifest_block(Path(args.manifest), notes)
    merged_slices, existing_floor, merged_notes, merged_unscorable = _merge_slices(
        out_path, args.tool, tool_cells, notes, unscorable, gold_sha
    )
    if codeql_packs is not None:
        packs_field: object = codeql_packs
    else:
        previous_packs = existing_floor.get("codeql_packs")
        packs_field = previous_packs if isinstance(previous_packs, dict) else None
    floor = {
        "session": "P001-S003",
        "generated_at": _now_iso(),
        "tool": args.tool,
        "gold": {"sha256": gold_sha, "n": len(records)},
        "unseen": {"source": args.unseen, "n": len(unseen_ids)},
        "lab_manifest": manifest_block,
        # Q3: registro das versões de configuração — versão dos query packs do
        # codeql lida do nome do diretório no cache; runs de semgrep/bandit
        # preservam o valor do floor.json existente (ou null)
        "codeql_packs": packs_field,
        "method": "pre-registered: plans/P001-S003-results.md (DR5)",
        "sets": {"defect": len(defect), "clean": len(clean), "excluded": excluded},
        "slices": merged_slices,
        "piso_det": _piso_det(merged_slices),
        "excluded_languages": excluded_languages,
        "unscorable": merged_unscorable,
        "notes": merged_notes,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(floor, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _print_summary(args.tool, floor, out_path, sum(built_counts.values()))


if __name__ == "__main__":
    main()
