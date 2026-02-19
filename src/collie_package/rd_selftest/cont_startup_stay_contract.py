"""cont_startup_stay Web Job contract (config + capability probe + manifest)."""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Literal, Protocol, TypedDict, TypeVar


JsonValue = (
    None
    | bool
    | int
    | float
    | str
    | Sequence['JsonValue']
    | Mapping[str, 'JsonValue']
)

T = TypeVar('T')


JOB_KIND: str = 'cont_startup_stay'
ARTIFACTS_MANIFEST_SCHEMA_VERSION: int = 1
ARTIFACTS_MANIFEST_FILENAME: str = 'artifacts_manifest.json'


class ArtifactsManifestV1(TypedDict):
    schema_version: int
    job_kind: str
    created_at: str
    status: Literal['started', 'completed']
    result: Literal['success', 'error', 'cancelled'] | None
    error: str | None
    traceback: str | None
    timestamp: str
    config: dict[str, JsonValue]
    capabilities: dict[str, JsonValue]
    degradation: dict[str, JsonValue]
    artifacts: list[dict[str, JsonValue]]

CollectorId = Literal[
    'logcat',
    'memcat',
    'meminfo',
    'vmstat',
    'greclaim_parm',
    'process_use_count',
    'oomadj',
    'ftrace',
]

PlannedStatus = Literal['enabled', 'disabled', 'skipped']
ArtifactStatus = Literal['planned', 'produced', 'missing', 'skipped']


@dataclass(frozen=True)
class OutputDirStrategy:
    dir_prefix: str = 'log_'
    timestamp_format: str = '%d_%H_%M'

    def format_timestamp(self, when: datetime | None = None) -> str:
        when = when or datetime.now()
        return when.strftime(self.timestamp_format)

    def render_dir_name(self, when: datetime | None = None) -> str:
        return f'{self.dir_prefix}{self.format_timestamp(when)}'

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            'dir_prefix': self.dir_prefix,
            'timestamp_format': self.timestamp_format,
        }


@dataclass(frozen=True)
class AppListSelection:
    preset_name: str | None = None
    custom_json: JsonValue | None = None

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            'preset_name': self.preset_name,
            'custom_json': self.custom_json,
        }


@dataclass(frozen=True)
class CollectorsConfig:
    logcat: bool = True
    memcat: bool = False
    meminfo: bool = True
    vmstat: bool = True
    greclaim_parm: bool = False
    process_use_count: bool = False
    oomadj: bool = False
    ftrace: bool = False
    ftrace_include_sched_switch: bool = False

    def iter_enabled_collectors(self) -> Iterable[CollectorId]:
        if self.logcat:
            yield 'logcat'
        if self.memcat:
            yield 'memcat'
        if self.meminfo:
            yield 'meminfo'
        if self.vmstat:
            yield 'vmstat'
        if self.greclaim_parm:
            yield 'greclaim_parm'
        if self.process_use_count:
            yield 'process_use_count'
        if self.oomadj:
            yield 'oomadj'
        if self.ftrace:
            yield 'ftrace'


BugreportMode = Literal['capture', 'skip']


@dataclass(frozen=True)
class BugreportPolicy:
    mode: BugreportMode = 'capture'
    cli_skip_window_sec: int = 10
    capture_timeout_sec: int = 1200


@dataclass(frozen=True)
class ContStartupStayConfig:
    device_id: str
    output_dir_strategy: OutputDirStrategy = field(default_factory=OutputDirStrategy)
    app_list: AppListSelection = field(default_factory=AppListSelection)
    collectors: CollectorsConfig = field(default_factory=CollectorsConfig)
    run_pre_start: bool = False
    bugreport: BugreportPolicy = field(default_factory=BugreportPolicy)

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            'device_id': self.device_id,
            'output_dir_strategy': self.output_dir_strategy.to_wire(),
            'app_list': self.app_list.to_wire(),
            'collectors': {
                'logcat': self.collectors.logcat,
                'memcat': self.collectors.memcat,
                'meminfo': self.collectors.meminfo,
                'vmstat': self.collectors.vmstat,
                'greclaim_parm': self.collectors.greclaim_parm,
                'process_use_count': self.collectors.process_use_count,
                'oomadj': self.collectors.oomadj,
                'ftrace': self.collectors.ftrace,
                'ftrace_include_sched_switch': self.collectors.ftrace_include_sched_switch,
            },
            'run_pre_start': self.run_pre_start,
            'bugreport': {
                'mode': self.bugreport.mode,
                'cli_skip_window_sec': self.bugreport.cli_skip_window_sec,
                'capture_timeout_sec': self.bugreport.capture_timeout_sec,
            },
        }


@dataclass(frozen=True)
class AdbShellResult:
    cmd: str
    returncode: int
    stdout: str = ''
    stderr: str = ''


class AdbLike(Protocol):
    def shell(self, cmd: str, timeout_sec: float = 20.0) -> AdbShellResult:
        raise NotImplementedError


@dataclass(frozen=True)
class DeviceCapabilities:
    root_available: bool
    root_probe_cmd: str
    root_probe_stdout: str
    has_greclaim_parm_node: bool
    has_process_use_count_node: bool
    tracing_base: str | None
    has_trace_pipe: bool

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            'root_available': self.root_available,
            'root_probe_cmd': self.root_probe_cmd,
            'root_probe_stdout': self.root_probe_stdout,
            'has_greclaim_parm_node': self.has_greclaim_parm_node,
            'has_process_use_count_node': self.has_process_use_count_node,
            'tracing_base': self.tracing_base,
            'has_trace_pipe': self.has_trace_pipe,
        }


def _has_uid0(text: str) -> bool:
    return 'uid=0' in (text or '')


def _probe_root(adb: AdbLike) -> tuple[bool, str, str]:
    candidates = [
        'id',
        'su -c id',
        'su 0 id',
    ]
    last_stdout = ''
    last_cmd = candidates[0]
    for cmd in candidates:
        last_cmd = cmd
        try:
            res = adb.shell(cmd, timeout_sec=10)
        except Exception as exc:  # noqa: BLE001
            last_stdout = f'<probe_error: {exc}>'
            continue
        last_stdout = (res.stdout or '').strip()
        if res.returncode == 0 and _has_uid0(last_stdout):
            return True, cmd, last_stdout
    return False, last_cmd, last_stdout


def _probe_test(adb: AdbLike, test_expr: str) -> bool:
    res = adb.shell(f'sh -c {json.dumps(test_expr)}', timeout_sec=10)
    return res.returncode == 0


def detect_device_capabilities(adb: AdbLike) -> DeviceCapabilities:
    root_ok, root_cmd, root_out = _probe_root(adb)

    has_greclaim = _probe_test(adb, 'test -r /sys/kernel/mi_reclaim/greclaim_parm')
    has_process_use_count = _probe_test(adb, 'test -r /sys/kernel/mi_mempool/process_use_count')

    tracing_base = None
    has_trace_pipe = False
    for base in ('/sys/kernel/tracing', '/sys/kernel/debug/tracing'):
        if _probe_test(adb, f'test -d {base}'):
            tracing_base = base
            has_trace_pipe = _probe_test(adb, f'test -r {base}/trace_pipe')
            break

    return DeviceCapabilities(
        root_available=root_ok,
        root_probe_cmd=root_cmd,
        root_probe_stdout=root_out,
        has_greclaim_parm_node=has_greclaim,
        has_process_use_count_node=has_process_use_count,
        tracing_base=tracing_base,
        has_trace_pipe=has_trace_pipe,
    )


@dataclass(frozen=True)
class PlannedCollector:
    collector_id: CollectorId
    requested: bool
    status: PlannedStatus
    reasons: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            'collector_id': self.collector_id,
            'requested': self.requested,
            'status': self.status,
            'reasons': list(self.reasons),
            'required_capabilities': list(self.required_capabilities),
        }


@dataclass(frozen=True)
class PlannedStep:
    step_id: str
    requested: bool
    status: PlannedStatus
    reasons: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            'step_id': self.step_id,
            'requested': self.requested,
            'status': self.status,
            'reasons': list(self.reasons),
            'required_capabilities': list(self.required_capabilities),
        }


@dataclass(frozen=True)
class ExecutionPlan:
    collectors: list[PlannedCollector]
    pre_start: PlannedStep
    bugreport: PlannedStep

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            'collectors': [c.to_wire() for c in self.collectors],
            'pre_start': self.pre_start.to_wire(),
            'bugreport': self.bugreport.to_wire(),
        }


def build_execution_plan(config: ContStartupStayConfig, caps: DeviceCapabilities) -> ExecutionPlan:
    planned: list[PlannedCollector] = []
    requested_by_id: dict[CollectorId, bool] = {
        'logcat': config.collectors.logcat,
        'memcat': config.collectors.memcat,
        'meminfo': config.collectors.meminfo,
        'vmstat': config.collectors.vmstat,
        'greclaim_parm': config.collectors.greclaim_parm,
        'process_use_count': config.collectors.process_use_count,
        'oomadj': config.collectors.oomadj,
        'ftrace': config.collectors.ftrace,
    }
    for cid in (
        'logcat',
        'memcat',
        'meminfo',
        'vmstat',
        'greclaim_parm',
        'process_use_count',
        'oomadj',
        'ftrace',
    ):
        requested = requested_by_id[cid]
        if not requested:
            planned.append(
                PlannedCollector(
                    collector_id=cid,
                    requested=False,
                    status='disabled',
                )
            )
            continue

        reasons: list[str] = []
        required: list[str] = []
        status: PlannedStatus = 'enabled'

        if cid == 'greclaim_parm':
            required = ['has_greclaim_parm_node']
            if not caps.has_greclaim_parm_node:
                status = 'skipped'
                reasons.append('missing_node:/sys/kernel/mi_reclaim/greclaim_parm')
        elif cid == 'process_use_count':
            required = ['has_process_use_count_node']
            if not caps.has_process_use_count_node:
                status = 'skipped'
                reasons.append('missing_node:/sys/kernel/mi_mempool/process_use_count')
        elif cid == 'ftrace':
            required = ['root_available', 'tracing_base', 'has_trace_pipe']
            if not caps.root_available:
                status = 'skipped'
                reasons.append('root_not_available')
            elif not caps.tracing_base:
                status = 'skipped'
                reasons.append('tracing_not_supported')
            elif not caps.has_trace_pipe:
                status = 'skipped'
                reasons.append('trace_pipe_not_readable')

        planned.append(
            PlannedCollector(
                collector_id=cid,
                requested=True,
                status=status,
                reasons=reasons,
                required_capabilities=required,
            )
        )

    pre_start_requested = bool(config.run_pre_start)
    pre_start_status: PlannedStatus = 'enabled' if pre_start_requested else 'disabled'
    pre_start_reasons: list[str] = []
    pre_start_required: list[str] = []
    if pre_start_requested and not caps.root_available:
        pre_start_status = 'skipped'
        pre_start_reasons = ['root_not_available']
        pre_start_required = ['root_available']

    bugreport_requested = config.bugreport.mode == 'capture'
    bugreport_status: PlannedStatus = 'enabled' if bugreport_requested else 'disabled'
    bugreport_reasons: list[str] = []
    bugreport_required: list[str] = []

    return ExecutionPlan(
        collectors=planned,
        pre_start=PlannedStep(
            step_id='pre_start',
            requested=pre_start_requested,
            status=pre_start_status,
            reasons=pre_start_reasons,
            required_capabilities=pre_start_required,
        ),
        bugreport=PlannedStep(
            step_id='bugreport',
            requested=bugreport_requested,
            status=bugreport_status,
            reasons=bugreport_reasons,
            required_capabilities=bugreport_required,
        ),
    )


@dataclass(frozen=True)
class ArtifactSpec:
    artifact_id: str
    description: str
    kind: str
    path_template: str | None = None
    path_glob: str | None = None
    relates_to: str | None = None


def default_artifact_specs() -> list[ArtifactSpec]:
    return [
        ArtifactSpec(
            artifact_id='console_log',
            description='Console tee log (Collie ConsoleLogger)',
            kind='log',
            path_template='console_{timestamp}.log',
        ),
        ArtifactSpec(
            artifact_id='logcat',
            description='Logcat capture output',
            kind='log',
            path_template='logcat_{timestamp}.txt',
            relates_to='logcat',
        ),
        ArtifactSpec(
            artifact_id='memcat_txt',
            description='Memcat text output',
            kind='data',
            path_template='memcat.txt',
            relates_to='memcat',
        ),
        ArtifactSpec(
            artifact_id='memcat_html',
            description='Memcat HTML report(s) if produced',
            kind='report',
            path_glob='memcat*.html',
            relates_to='memcat',
        ),
        ArtifactSpec(
            artifact_id='meminfo',
            description='dumpsys meminfo snapshot (before/after)',
            kind='data',
            path_template='meminfo{timestamp}.txt',
            relates_to='meminfo',
        ),
        ArtifactSpec(
            artifact_id='vmstat',
            description='vmstat snapshot (before/after)',
            kind='data',
            path_template='vmstat{timestamp}.txt',
            relates_to='vmstat',
        ),
        ArtifactSpec(
            artifact_id='greclaim_parm',
            description='greclaim_parm node dump (before/after)',
            kind='data',
            path_template='greclaim_parm{timestamp}.txt',
            relates_to='greclaim_parm',
        ),
        ArtifactSpec(
            artifact_id='process_use_count',
            description='process_use_count node dump (before/after)',
            kind='data',
            path_template='process_use_count{timestamp}.txt',
            relates_to='process_use_count',
        ),
        ArtifactSpec(
            artifact_id='oomadj_csv',
            description='OOMAdj CSV raw log',
            kind='data',
            path_template='oomadj_{timestamp}.csv',
            relates_to='oomadj',
        ),
        ArtifactSpec(
            artifact_id='oomadj_summary',
            description='OOMAdj summary report',
            kind='report',
            path_template='oomadj_summary_report_{timestamp}.txt',
            relates_to='oomadj',
        ),
        ArtifactSpec(
            artifact_id='ftrace_raw',
            description='ftrace raw capture',
            kind='data',
            path_template='ftrace_{timestamp}.txt',
            relates_to='ftrace',
        ),
        ArtifactSpec(
            artifact_id='direct_reclaim_report',
            description='ftrace direct reclaim parsed report',
            kind='report',
            path_template='ftrace_logs/direct_reclaim_report.txt',
            relates_to='ftrace',
        ),
        ArtifactSpec(
            artifact_id='kswapd_report',
            description='ftrace kswapd parsed report',
            kind='report',
            path_template='ftrace_logs/kswapd_report.txt',
            relates_to='ftrace',
        ),
        ArtifactSpec(
            artifact_id='bugreport_zip',
            description='Captured bugreport zip (if enabled)',
            kind='artifact',
            path_glob='bugreport_*.zip',
            relates_to='bugreport',
        ),
    ]


def _render_template(template: str, *, timestamp: str) -> str:
    return template.replace('{timestamp}', timestamp)


def build_artifacts_list(
    *,
    timestamp: str,
    plan: ExecutionPlan,
    specs: Sequence[ArtifactSpec] | None = None,


) -> list[dict[str, JsonValue]]:

    specs = list(specs) if specs is not None else default_artifact_specs()
    status_by_rel: dict[str, tuple[PlannedStatus, list[str]]] = {
        c.collector_id: (c.status, list(c.reasons)) for c in plan.collectors
    }
    status_by_rel['pre_start'] = (plan.pre_start.status, list(plan.pre_start.reasons))
    status_by_rel['bugreport'] = (plan.bugreport.status, list(plan.bugreport.reasons))

    artifacts: list[dict[str, JsonValue]] = []
    for spec in specs:
        related = spec.relates_to
        planned_status, reasons = status_by_rel.get(related or '', ('enabled', []))
        if related and planned_status in ('disabled', 'skipped'):
            artifacts.append(
                {
                    'artifact_id': spec.artifact_id,
                    'description': spec.description,
                    'kind': spec.kind,
                    'status': 'skipped',
                    'reasons': reasons or [f'{related}_not_enabled'],
                    'path': None,
                    'path_glob': spec.path_glob,
                    'path_template': spec.path_template,
                    'relates_to': related,
                }
            )
            continue

        artifacts.append(
            {
                'artifact_id': spec.artifact_id,
                'description': spec.description,
                'kind': spec.kind,
                'status': 'planned',
                'reasons': [],
                'path': _render_template(spec.path_template, timestamp=timestamp)
                if spec.path_template
                else None,
                'path_glob': spec.path_glob,
                'path_template': spec.path_template,
                'relates_to': related,
            }
        )

    return artifacts


def build_artifacts_manifest(
    *,
    config: ContStartupStayConfig,
    capabilities: DeviceCapabilities,
    plan: ExecutionPlan,
    timestamp: str,
    status: Literal['started', 'completed'] = 'started',
    result: Literal['success', 'error', 'cancelled'] | None = None,
    error: str | None = None,
    traceback_text: str | None = None,



) -> ArtifactsManifestV1:

    return {
        'schema_version': ARTIFACTS_MANIFEST_SCHEMA_VERSION,
        'job_kind': JOB_KIND,
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'status': status,
        'result': result,
        'error': error,
        'traceback': traceback_text,
        'timestamp': timestamp,
        'config': config.to_wire(),
        'capabilities': capabilities.to_wire(),
        'degradation': plan.to_wire(),
        'artifacts': build_artifacts_list(timestamp=timestamp, plan=plan),
    }


def write_artifacts_manifest(job_dir: Path, manifest: ArtifactsManifestV1) -> Path:
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / ARTIFACTS_MANIFEST_FILENAME
    _ = path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    return path


def run_and_write_manifest(
    *,
    job_dir: Path,
    config: ContStartupStayConfig,
    adb: AdbLike,
    fn: Callable[[], T],
    when: datetime | None = None,
    swallow_exception: bool = False,
    capabilities: DeviceCapabilities | None = None,
    plan: ExecutionPlan | None = None,


) -> T | None:

    timestamp = config.output_dir_strategy.format_timestamp(when)
    caps = capabilities or detect_device_capabilities(adb)
    exec_plan = plan or build_execution_plan(config, caps)

    started = build_artifacts_manifest(
        config=config,
        capabilities=caps,
        plan=exec_plan,
        timestamp=timestamp,
        status='started',
        result=None,
        error=None,
        traceback_text=None,
    )
    _ = write_artifacts_manifest(job_dir, started)

    try:
        out = fn()
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        completed = build_artifacts_manifest(
            config=config,
            capabilities=caps,
            plan=exec_plan,
            timestamp=timestamp,
            status='completed',
            result='error',
            error=str(exc),
            traceback_text=tb,
        )
        _ = write_artifacts_manifest(job_dir, completed)
        if swallow_exception:
            return None
        raise

    completed = build_artifacts_manifest(
        config=config,
        capabilities=caps,
        plan=exec_plan,
        timestamp=timestamp,
        status='completed',
        result='success',
        error=None,
        traceback_text=None,
    )
    _ = write_artifacts_manifest(job_dir, completed)
    return out


ARTIFACTS_MANIFEST_JSON_SCHEMA_V1: dict[str, JsonValue] = {
    'type': 'object',
    'required': [
        'schema_version',
        'job_kind',
        'created_at',
        'status',
        'timestamp',
        'config',
        'capabilities',
        'degradation',
        'artifacts',
    ],
    'properties': {
        'schema_version': {'type': 'integer', 'const': ARTIFACTS_MANIFEST_SCHEMA_VERSION},
        'job_kind': {'type': 'string', 'const': JOB_KIND},
        'created_at': {'type': 'string'},
        'status': {'type': 'string', 'enum': ['started', 'completed']},
        'result': {'type': ['string', 'null'], 'enum': ['success', 'error', 'cancelled', None]},
        'error': {'type': ['string', 'null']},
        'traceback': {'type': ['string', 'null']},
        'timestamp': {'type': 'string'},
        'config': {'type': 'object'},
        'capabilities': {'type': 'object'},
        'degradation': {'type': 'object'},
        'artifacts': {'type': 'array', 'items': {'type': 'object'}},
    },
}
