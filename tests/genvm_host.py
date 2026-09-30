"""
In-process GenVM host for testing GenLayer intelligent contracts.

What this is
------------
The contract under test is executed by the real GenLayer SDK. Storage goes
through the SDK's own slot engine, TreeMap and DynArray implementations,
`u32`/`u64`/`Address` coercion and `@allow_storage` descriptors. Nondeterministic
blocks go through the real `gl.vm.run_nondet_unsafe`, which cloudpickles the
leader and validator exactly as it does on chain, and this host then runs both
and applies the leader/validator consensus rule.

The only thing replaced is the node itself. GenVM reaches its host through five
wasi functions, and this module supplies them:

    gl_call(bytes) -> int      the request/reply channel, returns a readable fd
    storage_read(id, off, buf) fill buf from a slot
    storage_write(id, off, buf) write into a slot
    get_balance(addr) -> int
    get_self_balance() -> int

`gl_call` carries every host interaction as calldata, so this host dispatches on
the same request keys the node does: WebRequest, WebRender, ExecPrompt,
RunNondet, Sandbox, CallContract, PostMessage, EmitEvent, Trace.

Why it matters for consensus testing
------------------------------------
Because `RunNondet` arrives here as the two cloudpickled closures, this host can
run the leader, encode its result exactly as the node does (a one-byte result
code followed by calldata), hand that to the validator closure, and act on the
vote. Validator functions are therefore executed for real, against leader output
they did not produce, which is the only way to test an equivalence principle
without a network. Feeding the web and LLM channels different answers on the
leader's call and the validator's call reproduces genuine validator
disagreement.

What is faithfully modelled and what is not
-------------------------------------------
Modelled: storage layout and persistence, type coercion, revert semantics
(`gl.vm.UserError` unwinds and leaves no state change when the caller rolls the
snapshot back), leader/validator consensus on a single validator, cross-contract
view calls, the pinned transaction clock, and per-contract storage isolation.

Not modelled: gas metering, the multi-validator panel and its rotation or
appeal, and transaction finality. Those need a network, and the suite in
`tests/test_live_studionet.py` covers them against the deployed contract.
"""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import types
import typing
import warnings

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Locating the GenVM Python SDK
# ---------------------------------------------------------------------------

SDK_CACHE = Path.home() / ".cache" / "genvm-linter" / "extracted"


def find_sdk(version: str = "v0.2.16") -> Path:
    """
    Locate the py-genlayer standard library that the linter downloaded.

    The suite runs against the same SDK revision the contract header pins, so a
    local pass and an on-chain pass are exercising the same library.
    """
    base = SDK_CACHE / f"{version}.tar" / "py-lib-genlayer-std"
    if not base.is_dir():
        raise RuntimeError(
            f"GenVM SDK {version} is not in the linter cache at {base}. "
            f"Run: genvm-lint download --version {version}"
        )
    for candidate in sorted(base.iterdir()):
        if (candidate / "genlayer" / "__init__.py").is_file():
            return candidate
    raise RuntimeError(f"No genlayer package found under {base}")


# ---------------------------------------------------------------------------
# Host-side exceptions
# ---------------------------------------------------------------------------

class ConsensusFailure(Exception):
    """
    Raised when the validator closure rejects the leader's result.

    On chain this terminates the VM and the transaction produces no state
    change. Here it propagates out of the contract method, which has the same
    observable effect on storage because nothing was committed.
    """


class NoRouteError(Exception):
    """
    Raised when the contract reaches for a web page or a model that no test
    route covers. Failing loudly beats returning an empty body, which would
    silently test the wrong path.
    """


# ---------------------------------------------------------------------------
# Route table shared by the web and LLM channels
# ---------------------------------------------------------------------------

class _Route:
    """
    One registered answer for an outbound channel.

    `value` may be a plain answer, a list consumed in order (the last entry
    repeats once exhausted), or a callable taking the request. Lists are what
    make validator disagreement testable: the leader's call takes the first
    entry and the validator's call takes the second.
    """

    __slots__ = ("pattern", "value", "calls")

    def __init__(self, pattern: str, value: typing.Any):
        self.pattern = re.compile(pattern)
        self.value = value
        self.calls = 0

    def matches(self, subject: str) -> bool:
        return self.pattern.search(subject) is not None

    def take(self, subject: str) -> typing.Any:
        index = self.calls
        self.calls += 1
        value = self.value
        if isinstance(value, list):
            if not value:
                raise NoRouteError(f"route {self.pattern.pattern} has no answers left")
            return value[min(index, len(value) - 1)]
        if callable(value):
            return value(subject)
        return value


# ---------------------------------------------------------------------------
# The host
# ---------------------------------------------------------------------------

class GenVMHost:
    """
    A single in-process GenVM host. One instance backs a whole test session;
    `reset()` clears storage, routes and call logs between tests.
    """

    CHAIN_ID = 61999

    def __init__(self) -> None:
        self._sdk_path: Path | None = None
        self._modules: dict[str, types.ModuleType] = {}
        self.reset()

    # -- lifecycle ----------------------------------------------------------

    def reset(self) -> None:
        self._store: dict[tuple[bytes, bytes], bytearray] = {}
        self.contracts: dict[str, Deployed] = {}
        self.balances: dict[bytes, int] = {}
        self.web_routes: list[_Route] = []
        self.llm_routes: list[_Route] = []
        self.web_calls: list[dict] = []
        self.llm_calls: list[dict] = []
        self.rendered: list[str] = []
        self.posted_messages: list[dict] = []
        self.emitted_events: list[dict] = []
        self.nondet_runs: list[dict] = []
        self.skip_validator = False
        self._depth = 0
        self._snapshot: dict[tuple[bytes, bytes], bytearray] | None = None
        self._next_address = 1
        self.ctx: bytes = b"\x00" * 20
        self.now_ts: int | None = None
        self.clock_mode: str = "pinned"
        self._sender: bytes = b"\x00" * 20
        self._value: int = 0

    def clear_routes(self) -> None:
        self.web_routes = []
        self.llm_routes = []

    # -- route registration -------------------------------------------------

    def mock_web(self, pattern: str, value: typing.Any) -> None:
        """
        Answer web requests whose URL matches `pattern`.

        `value` is a dict with `status` and `body`, a list of such dicts
        consumed in order, or a callable taking the URL. A plain string or bytes
        is treated as a 200 with that body.
        """
        self.web_routes.append(_Route(pattern, value))

    def mock_llm(self, pattern: str, value: typing.Any) -> None:
        """
        Answer `exec_prompt` calls whose prompt matches `pattern`.

        `value` is a dict, a JSON string, a list consumed in order, or a
        callable taking the prompt. Passing a two-entry list is how a test makes
        the leader and the validator reach different conclusions.
        """
        self.llm_routes.append(_Route(pattern, value))

    # -- wasi surface -------------------------------------------------------

    def storage_read(self, slot_id: bytes, offset: int, buffer: bytearray) -> None:
        key = (self.ctx, bytes(slot_id))
        data = self._store.get(key)
        want = len(buffer)
        if data is None:
            buffer[:] = b"\x00" * want
            return
        chunk = bytes(data[offset : offset + want])
        if len(chunk) < want:
            chunk = chunk + b"\x00" * (want - len(chunk))
        buffer[:] = chunk

    def storage_write(self, slot_id: bytes, offset: int, payload: typing.Any) -> None:
        key = (self.ctx, bytes(slot_id))
        data = self._store.get(key)
        if data is None:
            data = bytearray()
            self._store[key] = data
        raw = bytes(payload)
        end = offset + len(raw)
        if len(data) < end:
            data.extend(b"\x00" * (end - len(data)))
        data[offset:end] = raw

    def get_balance(self, address: bytes) -> int:
        return int(self.balances.get(bytes(address), 0))

    def get_self_balance(self) -> int:
        return int(self.balances.get(self.ctx, 0))

    def gl_call(self, raw: bytes) -> int:
        calldata = self._calldata()
        request = calldata.decode(bytes(raw))
        if not isinstance(request, dict) or len(request) != 1:
            raise AssertionError(f"malformed host request: {request!r}")
        kind, payload = next(iter(request.items()))
        handler = getattr(self, f"_handle_{kind}", None)
        if handler is None:
            raise AssertionError(f"host request not supported by this harness: {kind}")
        reply = handler(payload)
        if reply is None:
            return 2**32 - 1
        return self._as_fd(reply)

    # -- host request handlers ---------------------------------------------

    def _handle_Trace(self, payload: typing.Any) -> bytes:
        return self._calldata().encode(0)

    def _handle_EmitEvent(self, payload: typing.Any) -> None:
        self.emitted_events.append(dict(payload) if isinstance(payload, dict) else {"raw": payload})
        return None

    def _handle_WebRequest(self, payload: dict) -> bytes:
        url = str(payload.get("url", ""))
        record = {
            "url": url,
            "method": str(payload.get("method", "GET")),
            "body": payload.get("body"),
        }
        self.web_calls.append(record)

        route = self._match(self.web_routes, url)
        if route is None:
            raise NoRouteError(f"no web route registered for {url}")
        answer = route.take(url)

        if isinstance(answer, Exception):
            return self._calldata().encode({"error": str(answer)})
        if isinstance(answer, (str, bytes, bytearray)):
            answer = {"status": 200, "body": answer}
        if not isinstance(answer, dict):
            raise NoRouteError(f"web route for {url} produced {type(answer).__name__}")

        body = answer.get("body", b"")
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = answer.get("headers") or {}
        encoded_headers = {
            str(k): (v.encode("utf-8") if isinstance(v, str) else bytes(v))
            for k, v in headers.items()
        }
        record["status"] = int(answer.get("status", 200))
        return self._calldata().encode(
            {
                "ok": {
                    "response": {
                        "status": int(answer.get("status", 200)),
                        "headers": encoded_headers,
                        "body": bytes(body),
                    }
                }
            }
        )

    def _handle_WebRender(self, payload: dict) -> bytes:
        url = str(payload.get("url", ""))
        self.rendered.append(url)
        route = self._match(self.web_routes, url)
        if route is None:
            raise NoRouteError(f"no web route registered for render of {url}")
        answer = route.take(url)
        if isinstance(answer, dict):
            answer = answer.get("body", "")
        if isinstance(answer, (bytes, bytearray)):
            answer = bytes(answer).decode("utf-8", errors="replace")
        return self._calldata().encode({"ok": {"text": str(answer)}})

    def _handle_ExecPrompt(self, payload: dict) -> bytes:
        prompt = str(payload.get("prompt", ""))
        self.llm_calls.append(
            {"prompt": prompt, "response_format": str(payload.get("response_format", "text"))}
        )
        route = self._match(self.llm_routes, prompt)
        if route is None:
            raise NoRouteError(
                f"no LLM route registered for prompt starting {prompt[:120]!r}"
            )
        answer = route.take(prompt)
        if isinstance(answer, Exception):
            return self._calldata().encode({"error": str(answer)})
        return self._calldata().encode({"ok": answer})

    def _handle_RunNondet(self, payload: dict) -> bytes:
        """
        Run a nondeterministic block the way the node does.

        The leader closure runs first and its outcome is encoded as a result
        code byte followed by calldata, which is exactly the byte string the
        node hands a validator in `stage_data['leaders_result']`. The validator
        closure is then executed against those bytes and its vote decides
        whether the block succeeds.

        Both closures arrive here as cloudpickle payloads produced by the real
        SDK, so a closure that accidentally captured storage would fail to
        unpickle cleanly here just as it would on chain.
        """
        import cloudpickle

        calldata = self._calldata()
        codes = self._result_codes()

        leader_fn = cloudpickle.loads(payload["data_leader"])
        validator_fn = cloudpickle.loads(payload["data_validator"])

        run: dict[str, typing.Any] = {"leader_value": None, "leader_error": None}
        try:
            value = leader_fn(None)
            encoded = bytes([codes.RETURN]) + calldata.encode(value)
            run["leader_value"] = value
        except ConsensusFailure:
            raise
        except NoRouteError:
            raise
        except Exception as exc:
            message = getattr(exc, "message", None) or str(exc)
            encoded = bytes([codes.USER_ERROR]) + str(message).encode("utf-8")
            run["leader_error"] = str(message)

        if self.skip_validator:
            agreed = True
        else:
            agreed = bool(validator_fn({"leaders_result": encoded}))
        run["validator_agreed"] = agreed
        self.nondet_runs.append(run)

        if not agreed:
            raise ConsensusFailure(
                "validator rejected the leader result: "
                + (run["leader_error"] or "leader returned a value the validator disagreed with")
            )
        return encoded

    def _handle_Sandbox(self, payload: dict) -> bytes:
        import cloudpickle

        calldata = self._calldata()
        codes = self._result_codes()
        fn = cloudpickle.loads(payload["data"])
        try:
            return bytes([codes.RETURN]) + calldata.encode(fn())
        except Exception as exc:
            message = getattr(exc, "message", None) or str(exc)
            return bytes([codes.USER_ERROR]) + str(message).encode("utf-8")

    def _handle_CallContract(self, payload: dict) -> bytes:
        """
        A synchronous view call into another deployed contract.

        The callee runs in its own storage namespace with `sender_address` set
        to the caller, which is what makes the cross-contract gate in the
        reference consumer testable end to end rather than by stub.
        """
        calldata = self._calldata()
        codes = self._result_codes()

        address = payload["address"]
        raw = getattr(address, "as_bytes", None)
        target_bytes = bytes(raw) if raw is not None else bytes(address)
        target_key = "0x" + target_bytes.hex()

        deployed = self.contracts.get(target_key)
        if deployed is None:
            return bytes([codes.USER_ERROR]) + f"no contract at {target_key}".encode()

        request = payload.get("calldata") or {}
        method = str(request.get("method", ""))
        args = list(request.get("args") or [])
        kwargs = dict(request.get("kwargs") or {})

        caller = self.ctx
        try:
            result = deployed.call(method, args, kwargs, sender=caller)
        except Exception as exc:
            message = getattr(exc, "message", None) or str(exc)
            return bytes([codes.USER_ERROR]) + str(message).encode("utf-8")
        return bytes([codes.RETURN]) + calldata.encode(result)

    def _handle_PostMessage(self, payload: dict) -> None:
        address = payload.get("address")
        raw = getattr(address, "as_bytes", None)
        self.posted_messages.append(
            {
                "address": "0x" + (bytes(raw) if raw is not None else b"").hex(),
                "calldata": payload.get("calldata"),
                "value": int(payload.get("value", 0) or 0),
                "on": str(payload.get("on", "finalized")),
                "from": "0x" + self.ctx.hex(),
            }
        )
        return None

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _match(routes: list[_Route], subject: str) -> _Route | None:
        for route in routes:
            if route.matches(subject):
                return route
        return None

    @staticmethod
    def _as_fd(data: bytes) -> int:
        """
        Hand the SDK a readable fd, which is what `gl_call` returns on chain.

        A temp file rather than a pipe, because the SDK reads the whole reply in
        one `os.fdopen(fd, 'rb').read()` and a reply larger than the pipe buffer
        would deadlock against a single-threaded writer.
        """
        fd, path = tempfile.mkstemp(prefix="genvm-host-")
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        readable = os.open(path, os.O_RDONLY)
        os.unlink(path)
        return readable

    def _calldata(self):
        return self._module("genlayer.py.calldata")

    def _result_codes(self):
        return self._module("genlayer.py.public_abi").ResultCode

    def _module(self, name: str):
        cached = self._modules.get(name)
        if cached is None:
            import importlib

            cached = importlib.import_module(name)
            self._modules[name] = cached
        return cached

    # -- addresses, senders, clock -----------------------------------------

    def new_address(self) -> str:
        """Allocate a deterministic test address."""
        value = self._next_address
        self._next_address += 1
        return "0x" + value.to_bytes(20, "big").hex()

    def warp(self, when: int | str | datetime | None) -> None:
        """
        Pin the transaction clock.

        GenVM pins `datetime.now()` to the transaction datetime so that leaders
        and validators read the same value. This reproduces that pinning, and
        `None` restores real time.
        """
        if when is None:
            self.now_ts = None
            return
        if isinstance(when, datetime):
            self.now_ts = int(when.timestamp())
        elif isinstance(when, str):
            text = when[:-1] + "+00:00" if when.endswith("Z") else when
            self.now_ts = int(datetime.fromisoformat(text).timestamp())
        else:
            self.now_ts = int(when)

    @contextmanager
    def context(self, contract: bytes, sender: bytes, value: int, is_init: bool = False):
        """
        Enter a contract call: switch the storage namespace, rebuild
        `gl.message` and `gl.message_raw`, and pin the clock.
        """
        genlayer_gl = self._module("genlayer.gl")
        types_mod = self._module("genlayer.py.types")

        previous = (self.ctx, self._sender, self._value)
        saved_message = genlayer_gl.message
        saved_raw = genlayer_gl.message_raw

        self.ctx = bytes(contract)
        self._sender = bytes(sender)
        self._value = int(value)

        stamp = self.now_ts if self.now_ts is not None else int(
            datetime.now(timezone.utc).timestamp()
        )
        iso = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        address_cls = types_mod.Address
        raw_message = {
            "contract_address": address_cls(self.ctx),
            "sender_address": address_cls(self._sender),
            "origin_address": address_cls(self._sender),
            "stack": [],
            "value": types_mod.u256(self._value),
            "datetime": iso,
            "is_init": bool(is_init),
            "chain_id": types_mod.u256(self.CHAIN_ID),
            "entry_kind": 0,
            "entry_data": b"",
            "entry_stage_data": None,
        }
        genlayer_gl.message_raw = raw_message
        genlayer_gl.message = type(saved_message)(
            contract_address=raw_message["contract_address"],
            sender_address=raw_message["sender_address"],
            origin_address=raw_message["origin_address"],
            value=raw_message["value"],
            chain_id=raw_message["chain_id"],
        )

        clock = None
        if self.clock_mode != "pinned":
            clock = _PinnedClock(stamp, self.clock_mode)
        elif self.now_ts is not None:
            clock = _PinnedClock(stamp, "pinned")
        patched: list[tuple[types.ModuleType, typing.Any]] = []
        if clock is not None:
            for module in self._contract_modules():
                if hasattr(module, "datetime"):
                    patched.append((module, module.datetime))
                    module.datetime = clock
        try:
            yield
        finally:
            for module, original in patched:
                module.datetime = original
            genlayer_gl.message = saved_message
            genlayer_gl.message_raw = saved_raw
            self.ctx, self._sender, self._value = previous

    @contextmanager
    def transaction(self):
        """
        Apply GenVM revert semantics to the outermost call.

        A transaction that fails applies no state change on chain, so the whole
        storage image is snapshotted when the outermost call begins and restored
        if that call raises. Nested calls, such as a synchronous view into another
        contract, join the enclosing transaction rather than starting their own,
        which is what lets a failing inner view be caught and handled without
        discarding the caller's writes.
        """
        outermost = self._depth == 0
        if outermost:
            self._snapshot = {key: bytearray(value) for key, value in self._store.items()}
        self._depth += 1
        try:
            yield
        except BaseException:
            if outermost and self._snapshot is not None:
                self._store = {key: bytearray(value) for key, value in self._snapshot.items()}
            raise
        finally:
            self._depth -= 1
            if outermost:
                self._snapshot = None

    def _contract_modules(self) -> list[types.ModuleType]:
        seen: list[types.ModuleType] = []
        for deployed in self.contracts.values():
            if deployed.module not in seen:
                seen.append(deployed.module)
        return seen

    # -- deployment --------------------------------------------------------

    def deploy(
        self,
        source: str | Path,
        class_name: str,
        args: typing.Sequence[typing.Any] = (),
        sender: str | None = None,
        address: str | None = None,
    ) -> "Deployed":
        """
        Load a contract module, bind it to a fresh storage namespace and run its
        constructor, mirroring what the node does on a deploy transaction:
        `Root.get()`, `lock_default()`, then the contract's `__init__`.
        """
        module = self._load_module(Path(source))
        contract_cls = getattr(module, class_name)

        contract_address = address or self.new_address()
        address_bytes = bytes.fromhex(contract_address[2:])
        sender_bytes = bytes.fromhex((sender or self.new_address())[2:])

        storage_mod = self._module("genlayer.py.storage")
        with self.transaction(), self.context(address_bytes, sender_bytes, 0, is_init=True):
            root = storage_mod.Root.get()
            root.lock_default()
            instance = root.get_contract_instance(contract_cls)
            contract_cls.__init__(instance, *args)

        deployed = Deployed(self, contract_address, contract_cls, module)
        self.contracts["0x" + address_bytes.hex()] = deployed
        return deployed

    def _load_module(self, path: Path) -> types.ModuleType:
        import importlib.util

        resolved = path.resolve()
        name = "gvm_contract_" + resolved.stem
        existing = sys.modules.get(name)
        if existing is not None:
            return existing

        spec = importlib.util.spec_from_file_location(name, resolved)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load contract module from {resolved}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module

        # The SDK allows one gl.Contract subclass per GenVM instance, and on
        # chain every contract has its own instance. This harness loads several
        # contract modules into one process, so the registration slot is cleared
        # before each module is executed. Two contracts declared in the same
        # file still trip the check, which is the rule that actually matters.
        contracts_mod = self._module("genlayer.gl.genvm_contracts")
        contracts_mod.__known_contract__ = None
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        return module


class _PinnedClock:
    """
    Stands in for `datetime` inside a contract module so `datetime.now(tz)`
    returns the transaction time, which is what GenVM guarantees on chain.
    Every other attribute is forwarded to the real class.

    Two failure modes exist so the contract's clock fallbacks can be exercised:
    `broken_now` makes `now()` raise while leaving `fromisoformat` working, which
    drives the contract onto the transaction datetime carried on the message;
    `dead` breaks both, which drives it onto its fail-closed default.
    """

    __slots__ = ("_stamp", "_mode")

    def __init__(self, stamp: int, mode: str = "pinned"):
        self._stamp = stamp
        self._mode = mode

    def now(self, tz=None):
        if self._mode in ("broken_now", "dead"):
            raise RuntimeError("pinned clock unavailable")
        return datetime.fromtimestamp(self._stamp, tz or timezone.utc)

    def fromisoformat(self, value):
        if self._mode == "dead":
            raise RuntimeError("datetime parsing unavailable")
        return datetime.fromisoformat(value)

    def fromtimestamp(self, stamp, tz=None):
        return datetime.fromtimestamp(stamp, tz)

    def __getattr__(self, name):
        return getattr(datetime, name)


class Deployed:
    """
    A deployed contract. Attribute access returns a bound caller, so a test
    reads like the on-chain call it stands for:

        guard.register_charter("acme", "Acme", url, 0, False, True, sender=alice)
    """

    def __init__(self, host: GenVMHost, address: str, contract_cls: type, module: types.ModuleType):
        self.host = host
        self.address = address
        self.address_bytes = bytes.fromhex(address[2:])
        self.contract_cls = contract_cls
        self.module = module

    def call(
        self,
        method: str,
        args: typing.Sequence[typing.Any] = (),
        kwargs: dict[str, typing.Any] | None = None,
        sender: str | bytes | None = None,
        value: int = 0,
    ) -> typing.Any:
        if sender is None:
            sender_bytes = self.host._sender
        elif isinstance(sender, bytes):
            sender_bytes = sender
        else:
            sender_bytes = bytes.fromhex(str(sender)[2:])

        storage_mod = self.host._module("genlayer.py.storage")
        with self.host.transaction():
            with self.host.context(self.address_bytes, sender_bytes, value):
                instance = storage_mod.Root.get().get_contract_instance(self.contract_cls)
                bound = getattr(type(instance), method, None)
                if bound is None:
                    raise AttributeError(f"{self.contract_cls.__name__} has no method {method}")
                return bound(instance, *args, **(kwargs or {}))

    def __getattr__(self, method: str):
        if method.startswith("_"):
            raise AttributeError(method)

        def invoke(*args, sender: str | bytes | None = None, value: int = 0, **kwargs):
            return self.call(method, args, kwargs, sender=sender, value=value)

        return invoke


# ---------------------------------------------------------------------------
# Bootstrap: install the wasi stub, then import the real SDK against it
# ---------------------------------------------------------------------------

_HOST: GenVMHost | None = None


def bootstrap(version: str = "v0.2.16") -> GenVMHost:
    """
    Prepare the process so the real GenLayer SDK can be imported and run.

    Three steps, in this order, because each depends on the last:

    1. Put a `_genlayer_wasi` module on `sys.modules` that forwards the five
       host functions to the singleton host. Every SDK module that touches the
       node imports this by name at import time.
    2. Place the calldata-encoded transaction message on file descriptor 0,
       because `genlayer._internal.msg` decodes `message_raw` from fd 0 the
       moment it is imported. The descriptor is restored immediately after.
    3. Import `genlayer.gl`, which wires `Root.MANAGER` to the wasi-backed
       storage manager and builds the initial `gl.message`.
    """
    global _HOST
    if _HOST is not None:
        return _HOST

    host = GenVMHost()
    sdk_path = find_sdk(version)
    host._sdk_path = sdk_path
    if str(sdk_path) not in sys.path:
        sys.path.insert(0, str(sdk_path))

    wasi = types.ModuleType("_genlayer_wasi")
    wasi.gl_call = lambda data: host.gl_call(data)
    wasi.storage_read = lambda slot, off, buf: host.storage_read(slot, off, buf)
    wasi.storage_write = lambda slot, off, buf: host.storage_write(slot, off, buf)
    wasi.get_balance = lambda addr: host.get_balance(addr)
    wasi.get_self_balance = lambda: host.get_self_balance()
    sys.modules["_genlayer_wasi"] = wasi

    import importlib

    calldata = importlib.import_module("genlayer.py.calldata")
    types_mod = importlib.import_module("genlayer.py.types")

    zero = types_mod.Address(b"\x00" * 20)
    boot_message = {
        "contract_address": zero,
        "sender_address": zero,
        "origin_address": zero,
        "stack": [],
        "value": types_mod.u256(0),
        "datetime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "is_init": False,
        "chain_id": types_mod.u256(GenVMHost.CHAIN_ID),
        "entry_kind": 0,
        "entry_data": b"",
        "entry_stage_data": None,
    }

    encoded = calldata.encode(boot_message)
    fd, path = tempfile.mkstemp(prefix="genvm-msg-")
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)

    saved_stdin = os.dup(0)
    message_fd = os.open(path, os.O_RDONLY)
    try:
        os.dup2(message_fd, 0)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            importlib.import_module("genlayer.gl")
            importlib.import_module("genlayer")
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
        os.close(message_fd)
        os.unlink(path)

    _HOST = host
    return host
