from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol, Sequence


DEVICE_ID_RE = re.compile(r'^[A-Za-z0-9._:-]+$')
PACKAGE_NAME_RE = re.compile(r'^[A-Za-z0-9_.]+$')

COMPILE_MODES_ALLOWED = {
    'speed-profile',
    'speed',
    'quicken',
    'everything',
    'verify',
    'interpret-only',
}


class AdbExecutorError(RuntimeError):
    pass


class MissingDeviceIdError(AdbExecutorError):
    pass


class InvalidDeviceIdError(AdbExecutorError):
    pass


class InvalidAdbArgsError(AdbExecutorError):
    pass


@dataclass(frozen=True)
class AdbExecResult:
    argv: list[str]
    returncode: int
    stdout: str = ''
    stderr: str = ''
    elapsed_sec: float = 0.0


class AdbExecutorLike(Protocol):
    def build_argv(self, device_id: str, args: Sequence[str]) -> list[str]:
        raise NotImplementedError

    def build_host_argv(self, args: Sequence[str]) -> list[str]:
        raise NotImplementedError

    def run(self, device_id: str, args: Sequence[str], timeout_sec: float = 20.0) -> AdbExecResult:
        raise NotImplementedError

    def run_host(self, args: Sequence[str], timeout_sec: float = 20.0) -> AdbExecResult:
        raise NotImplementedError


def validate_device_id(device_id: str) -> str:
    device_id = (device_id or '').strip()
    if not device_id:
        raise MissingDeviceIdError('device_id 不能为空')
    if not DEVICE_ID_RE.fullmatch(device_id):
        raise InvalidDeviceIdError('device_id 含非法字符')
    return device_id


def validate_package_name(package_name: str) -> str:
    package_name = (package_name or '').strip()
    if not package_name or not PACKAGE_NAME_RE.fullmatch(package_name):
        raise AdbExecutorError('无效包名')
    return package_name


def validate_compile_mode(mode: str) -> str:
    mode = (mode or '').strip() or 'speed-profile'
    if mode not in COMPILE_MODES_ALLOWED:
        raise AdbExecutorError('mode 不合法')
    return mode


def _validate_device_args(args: Sequence[str]) -> list[str]:
    if not args:
        raise InvalidAdbArgsError('args 不能为空')
    argv = [str(x) for x in args]
    head = (argv[0] or '').strip()
    if not head:
        raise InvalidAdbArgsError('args[0] 不能为空')

    # 阻断注入：禁止在子命令前注入 adb 全局参数（例如 -s/-H/-P/...）。
    if head.startswith('-'):
        raise InvalidAdbArgsError('不允许以 "-" 开头的 adb 参数')
    if head == 'adb':
        raise InvalidAdbArgsError('args 不应包含 adb 本体')
    return argv


class SubprocessAdbExecutor:
    def __init__(self, adb_path: str = 'adb') -> None:
        self._adb_path = adb_path

    def build_argv(self, device_id: str, args: Sequence[str]) -> list[str]:
        device_id = validate_device_id(device_id)
        tail = _validate_device_args(args)
        return [self._adb_path, '-s', device_id, *tail]

    def build_host_argv(self, args: Sequence[str]) -> list[str]:
        if not args:
            raise InvalidAdbArgsError('args 不能为空')
        tail = [str(x) for x in args]
        head = (tail[0] or '').strip()
        if not head:
            raise InvalidAdbArgsError('args[0] 不能为空')
        if head == 'adb':
            raise InvalidAdbArgsError('args 不应包含 adb 本体')
        return [self._adb_path, *tail]

    def run(self, device_id: str, args: Sequence[str], timeout_sec: float = 20.0) -> AdbExecResult:
        argv = self.build_argv(device_id=device_id, args=args)
        start = time.monotonic()
        cp = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        return AdbExecResult(
            argv=argv,
            returncode=cp.returncode,
            stdout=cp.stdout or '',
            stderr=cp.stderr or '',
            elapsed_sec=time.monotonic() - start,
        )

    def run_host(self, args: Sequence[str], timeout_sec: float = 20.0) -> AdbExecResult:
        argv = self.build_host_argv(args=args)
        start = time.monotonic()
        cp = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        return AdbExecResult(
            argv=argv,
            returncode=cp.returncode,
            stdout=cp.stdout or '',
            stderr=cp.stderr or '',
            elapsed_sec=time.monotonic() - start,
        )
