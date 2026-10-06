#!/usr/bin/env python3
"""E10: experimento reproduzível de concorrência/idempotência em SQLite."""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent.parent
SPIKE_DIR = Path(__file__).resolve().parent
SEED_RESERVAS = ROOT / "dados" / "reservas.json"
SEED_AREAS = ROOT / "dados" / "areas.json"
SEED_APARTAMENTOS = ROOT / "dados" / "apartamentos.json"

STRATEGIES = ("E0", "E1", "E2", "E3")
MODES = (
    "threads-default",
    "spawn-default",
    "spawn-zero",
    "subprocess-default",
    "subprocess-zero",
    "asyncio-default",
)
CHEAP_SCENARIO_N = {"C1": 300, "C2": 300, "C3": 300, "C4": 300, "C5": 300}
EXPENSIVE_SCENARIO_N = {"C1": 60, "C2": 60, "C3": 60, "C4": 40, "C5": 60}
MAX_LOCK_RETRIES = 80
PROCESS_LOCK = threading.Lock()


def public_reserved(code: str) -> dict[str, str]:
    return {"status": "reservada", "codigo": code}


def public_already(code: str) -> dict[str, str]:
    return {"status": "ja_reservada", "codigo": code}


def public_refused() -> dict[str, str]:
    return {"status": "recusada"}


def is_locked(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


class ReservationStore:
    """Uma chamada pública sempre retorna um dos três resultados do contrato."""

    def __init__(self, db_path: str, strategy: str, timeout_mode: str) -> None:
        self.db_path = db_path
        self.strategy = strategy
        self.timeout_mode = timeout_mode
        self.last_infra_error: str | None = None

    @property
    def table(self) -> str:
        return f"reservas_{self.strategy.lower()}"

    @property
    def sequence_name(self) -> str:
        return self.strategy.lower()

    def connect(self) -> sqlite3.Connection:
        if self.timeout_mode == "zero":
            connection = sqlite3.connect(
                self.db_path, timeout=0.0, isolation_level=None
            )
            connection.execute("PRAGMA busy_timeout=0")
        else:
            connection = sqlite3.connect(self.db_path, isolation_level=None)
        return connection

    def next_code(self, connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "UPDATE code_sequences SET value=value+1 WHERE name=? RETURNING value",
            (self.sequence_name,),
        ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError("sequência ausente")
        return f"RSV-{row[0]}"

    def transaction_with_retry(
        self, operation: Callable[[sqlite3.Connection], dict[str, str]]
    ) -> dict[str, str]:
        last_error: BaseException | None = None
        for attempt in range(MAX_LOCK_RETRIES + 1):
            connection = self.connect()
            try:
                return operation(connection)
            except BaseException as exc:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                if is_locked(exc) and attempt < MAX_LOCK_RETRIES:
                    last_error = exc
                    time.sleep(min(0.0005 * (attempt + 1), 0.010))
                    continue
                raise
            finally:
                connection.close()
        raise sqlite3.OperationalError(str(last_error or "database is locked"))

    def existing(
        self, connection: sqlite3.Connection, apartamento: str, area: str, data: str
    ) -> tuple[str, str] | None:
        row = connection.execute(
            f"SELECT apartamento, codigo FROM {self.table} "
            "WHERE area=? AND data=? AND ativa=1 ORDER BY id LIMIT 1",
            (area, data),
        ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def unsafe_select_then_insert(
        self, apartamento: str, area: str, data: str
    ) -> dict[str, str]:
        connection = self.connect()
        try:
            found = self.existing(connection, apartamento, area, data)
        finally:
            connection.close()
        if found:
            return public_already(found[1]) if found[0] == apartamento else public_refused()

        def insert(connection: sqlite3.Connection) -> dict[str, str]:
            connection.execute("BEGIN IMMEDIATE")
            code = self.next_code(connection)
            connection.execute(
                f"INSERT INTO {self.table}"
                "(codigo, apartamento, area, data, ativa) VALUES (?, ?, ?, ?, 1)",
                (code, apartamento, area, data),
            )
            connection.commit()
            return public_reserved(code)

        return self.transaction_with_retry(insert)

    def unique_insert(
        self, apartamento: str, area: str, data: str
    ) -> dict[str, str]:
        def operation(connection: sqlite3.Connection) -> dict[str, str]:
            connection.execute("BEGIN")
            same = connection.execute(
                f"SELECT codigo FROM {self.table} "
                "WHERE apartamento=? AND area=? AND data=? AND ativa=1",
                (apartamento, area, data),
            ).fetchone()
            if same:
                connection.commit()
                return public_already(str(same[0]))
            code = self.next_code(connection)
            try:
                connection.execute(
                    f"INSERT INTO {self.table}"
                    "(codigo, apartamento, area, data, ativa) VALUES (?, ?, ?, ?, 1)",
                    (code, apartamento, area, data),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                same_after = connection.execute(
                    f"SELECT codigo FROM {self.table} "
                    "WHERE apartamento=? AND area=? AND data=? AND ativa=1",
                    (apartamento, area, data),
                ).fetchone()
                return (
                    public_already(str(same_after[0]))
                    if same_after
                    else public_refused()
                )
            connection.commit()
            return public_reserved(code)

        return self.transaction_with_retry(operation)

    def immediate_check_insert(
        self, apartamento: str, area: str, data: str
    ) -> dict[str, str]:
        def operation(connection: sqlite3.Connection) -> dict[str, str]:
            connection.execute("BEGIN IMMEDIATE")
            found = self.existing(connection, apartamento, area, data)
            if found:
                connection.commit()
                return (
                    public_already(found[1])
                    if found[0] == apartamento
                    else public_refused()
                )
            code = self.next_code(connection)
            connection.execute(
                f"INSERT INTO {self.table}"
                "(codigo, apartamento, area, data, ativa) VALUES (?, ?, ?, ?, 1)",
                (code, apartamento, area, data),
            )
            connection.commit()
            return public_reserved(code)

        return self.transaction_with_retry(operation)

    def gravar_reserva(
        self, apartamento: str, area: str, data: str
    ) -> dict[str, str]:
        self.last_infra_error = None
        try:
            if self.strategy == "E0":
                return self.unsafe_select_then_insert(apartamento, area, data)
            if self.strategy == "E1":
                return self.unique_insert(apartamento, area, data)
            if self.strategy == "E2":
                return self.immediate_check_insert(apartamento, area, data)
            if self.strategy == "E3":
                with PROCESS_LOCK:
                    return self.unsafe_select_then_insert(apartamento, area, data)
            raise ValueError(f"estratégia desconhecida: {self.strategy}")
        except BaseException as exc:
            # A fronteira pública nunca propaga falha de infraestrutura.
            self.last_infra_error = f"{type(exc).__name__}: {exc}"
            return public_refused()


def perform_call(
    db_path: str, strategy: str, timeout_mode: str, request: dict[str, str]
) -> dict[str, Any]:
    store = ReservationStore(db_path, strategy, timeout_mode)
    result = store.gravar_reserva(
        request["apartamento"], request["area"], request["data"]
    )
    return {"result": result, "infra_error": store.last_infra_error}


class PairRunner:
    def run(
        self,
        db_path: str,
        strategy: str,
        timeout_mode: str,
        requests: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class ThreadRunner(PairRunner):
    def run(
        self,
        db_path: str,
        strategy: str,
        timeout_mode: str,
        requests: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        barrier = threading.Barrier(3)
        results: list[dict[str, Any] | None] = [None, None]

        def target(index: int) -> None:
            barrier.wait()
            results[index] = perform_call(
                db_path, strategy, timeout_mode, requests[index]
            )

        threads = [
            threading.Thread(target=target, args=(index,))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        return [item for item in results if item is not None]


class AsyncRunner(PairRunner):
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()

    def run(
        self,
        db_path: str,
        strategy: str,
        timeout_mode: str,
        requests: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        async def pair() -> list[dict[str, Any]]:
            barrier = asyncio.Barrier(3)

            async def call(index: int) -> dict[str, Any]:
                await barrier.wait()
                return await asyncio.to_thread(
                    perform_call,
                    db_path,
                    strategy,
                    timeout_mode,
                    requests[index],
                )

            tasks = [asyncio.create_task(call(0)), asyncio.create_task(call(1))]
            await barrier.wait()
            return list(await asyncio.gather(*tasks))

        return self.loop.run_until_complete(pair())

    def close(self) -> None:
        self.loop.close()


def process_worker(
    barrier: Any, input_queue: Any, output_queue: Any
) -> None:
    while True:
        job = input_queue.get()
        if job is None:
            return
        barrier.wait()
        try:
            response = perform_call(**job)
        except BaseException as exc:
            response = {
                "result": public_refused(),
                "infra_error": f"worker {type(exc).__name__}: {exc}",
            }
        output_queue.put(response)


class SpawnRunner(PairRunner):
    def __init__(self) -> None:
        context = multiprocessing.get_context("spawn")
        self.barrier = context.Barrier(3)
        self.inputs = [context.Queue(), context.Queue()]
        self.outputs = [context.Queue(), context.Queue()]
        self.processes = [
            context.Process(
                target=process_worker,
                args=(self.barrier, self.inputs[index], self.outputs[index]),
            )
            for index in range(2)
        ]
        for process in self.processes:
            process.start()

    def run(
        self,
        db_path: str,
        strategy: str,
        timeout_mode: str,
        requests: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        for index in range(2):
            self.inputs[index].put(
                {
                    "db_path": db_path,
                    "strategy": strategy,
                    "timeout_mode": timeout_mode,
                    "request": requests[index],
                }
            )
        self.barrier.wait()
        results = []
        for output in self.outputs:
            try:
                results.append(output.get(timeout=30))
            except queue.Empty:
                results.append(
                    {
                        "result": public_refused(),
                        "infra_error": "spawn worker timeout",
                    }
                )
        return results

    def close(self) -> None:
        for input_queue in self.inputs:
            input_queue.put(None)
        for process in self.processes:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        for item in self.inputs + self.outputs:
            item.close()


def subprocess_worker_main() -> int:
    for line in sys.stdin:
        job = json.loads(line)
        print(json.dumps({"state": "ready"}), flush=True)
        command = sys.stdin.readline()
        if not command:
            return 1
        if json.loads(command).get("command") != "go":
            return 2
        try:
            response = perform_call(**job)
        except BaseException as exc:
            response = {
                "result": public_refused(),
                "infra_error": f"subprocess worker {type(exc).__name__}: {exc}",
            }
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


class SubprocessRunner(PairRunner):
    def __init__(self) -> None:
        self.processes = [
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--subprocess-worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            for _ in range(2)
        ]

    def run(
        self,
        db_path: str,
        strategy: str,
        timeout_mode: str,
        requests: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        for index, process in enumerate(self.processes):
            assert process.stdin is not None
            process.stdin.write(
                json.dumps(
                    {
                        "db_path": db_path,
                        "strategy": strategy,
                        "timeout_mode": timeout_mode,
                        "request": requests[index],
                    }
                )
                + "\n"
            )
            process.stdin.flush()
        for process in self.processes:
            assert process.stdout is not None
            ready = json.loads(process.stdout.readline())
            if ready != {"state": "ready"}:
                raise RuntimeError(f"barreira de subprocesso inválida: {ready}")
        # Barreira coordenada pelo processo pai por dois pipes independentes.
        for process in self.processes:
            assert process.stdin is not None
            process.stdin.write('{"command":"go"}\n')
            process.stdin.flush()
        return [
            json.loads(process.stdout.readline())  # type: ignore[union-attr]
            for process in self.processes
        ]

    def close(self) -> None:
        for process in self.processes:
            if process.stdin:
                process.stdin.close()
        for process in self.processes:
            process.wait(timeout=5)


def initialize_database(
    db_path: Path,
    reservas: list[dict[str, str]],
) -> None:
    maximum = max(int(item["codigo"].split("-")[1]) for item in reservas)
    connection = sqlite3.connect(db_path, isolation_level=None)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(
            "CREATE TABLE code_sequences(name TEXT PRIMARY KEY, value INTEGER NOT NULL)"
        )
        for strategy in STRATEGIES:
            table = f"reservas_{strategy.lower()}"
            connection.execute(
                f"CREATE TABLE {table}("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "codigo TEXT NOT NULL UNIQUE,"
                "apartamento TEXT NOT NULL,"
                "area TEXT NOT NULL,"
                "data TEXT NOT NULL,"
                "ativa INTEGER NOT NULL CHECK (ativa IN (0,1)))"
            )
            if strategy == "E1":
                connection.execute(
                    f"CREATE UNIQUE INDEX ux_{table}_ativa "
                    f"ON {table}(area, data) WHERE ativa=1"
                )
            connection.execute(
                "INSERT INTO code_sequences(name, value) VALUES (?, ?)",
                (strategy.lower(), maximum),
            )
            connection.executemany(
                f"INSERT INTO {table}"
                "(codigo, apartamento, area, data, ativa) VALUES (?, ?, ?, ?, 1)",
                [
                    (
                        item["codigo"],
                        item["apartamento"],
                        item["area"],
                        item["data"],
                    )
                    for item in reservas
                ],
            )
    finally:
        connection.close()


def active_rows(
    db_path: str, strategy: str, area: str, data_value: str
) -> list[dict[str, Any]]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            f"SELECT codigo, apartamento, area, data, ativa "
            f"FROM reservas_{strategy.lower()} "
            "WHERE area=? AND data=? AND ativa=1 ORDER BY codigo",
            (area, data_value),
        ).fetchall()
        return [
            {
                "codigo": row[0],
                "apartamento": row[1],
                "area": row[2],
                "data": row[3],
                "ativa": row[4],
            }
            for row in rows
        ]
    finally:
        connection.close()


def all_codes(db_path: str, strategy: str) -> set[str]:
    connection = sqlite3.connect(db_path)
    try:
        return {
            str(row[0])
            for row in connection.execute(
                f"SELECT codigo FROM reservas_{strategy.lower()}"
            )
        }
    finally:
        connection.close()


def cancel_code(db_path: str, strategy: str, code: str) -> None:
    connection = sqlite3.connect(db_path, isolation_level=None)
    try:
        connection.execute(
            f"UPDATE reservas_{strategy.lower()} SET ativa=0 WHERE codigo=?",
            (code,),
        )
    finally:
        connection.close()


def generated_date(strategy: str, mode: str, scenario: str, index: int) -> str:
    strategy_offset = STRATEGIES.index(strategy) * 60
    mode_offset = MODES.index(mode) * 10
    scenario_offset = int(scenario[1:]) * 500
    start = date(2040 + strategy_offset + mode_offset, 1, 1)
    return (start + timedelta(days=scenario_offset + index)).isoformat()


def scenario_n(mode: str, scenario: str) -> int:
    matrix = (
        CHEAP_SCENARIO_N
        if mode.startswith(("threads", "asyncio"))
        else EXPENSIVE_SCENARIO_N
    )
    return matrix[scenario]


@dataclass
class ScenarioResult:
    failures: int = 0
    infra_errors: int = 0
    first_failure: dict[str, Any] | None = None
    first_pass: dict[str, Any] | None = None

    def observe(self, passed: bool, detail: dict[str, Any]) -> None:
        errors = sum(1 for item in detail["responses"] if item.get("infra_error"))
        self.infra_errors += errors
        if errors:
            passed = False
        if passed and self.first_pass is None:
            self.first_pass = detail
        if not passed:
            self.failures += 1
            if self.first_failure is None:
                self.first_failure = detail


def run_scenario(
    db_path: str,
    runner: PairRunner,
    strategy: str,
    mode: str,
    scenario: str,
    areas: list[str],
    apartamentos: list[str],
    seeds: list[dict[str, str]],
) -> ScenarioResult:
    timeout_mode = "zero" if mode.endswith("-zero") else "default"
    result = ScenarioResult()
    for index in range(scenario_n(mode, scenario)):
        data_value = generated_date(strategy, mode, scenario, index)
        area = areas[index % len(areas)]
        apt_a, apt_b = apartamentos[0], apartamentos[1]

        if scenario == "C2":
            apt_b = apt_a
        if scenario == "C3":
            seed = seeds[index % len(seeds)]
            area, data_value = seed["area"], seed["data"]
            candidates = [
                apartment
                for apartment in apartamentos
                if apartment != seed["apartamento"]
            ]
            apt_a, apt_b = candidates[0], candidates[1]
        second_area = areas[(index + 1) % len(areas)]
        requests = [
            {"apartamento": apt_a, "area": area, "data": data_value},
            {
                "apartamento": apt_b,
                "area": second_area if scenario == "C5" else area,
                "data": data_value,
            },
        ]
        responses = runner.run(
            db_path, strategy, timeout_mode, requests
        )
        rows = active_rows(db_path, strategy, area, data_value)
        public = [item["result"] for item in responses]
        statuses = sorted(item["status"] for item in public)
        passed = False

        if scenario == "C1":
            passed = (
                statuses == ["recusada", "reservada"]
                and len(rows) == 1
                and sum(item == public_refused() for item in public) == 1
            )
        elif scenario == "C2":
            reserved = [item for item in public if item["status"] == "reservada"]
            repeated = [
                item for item in public if item["status"] == "ja_reservada"
            ]
            passed = (
                len(reserved) == 1
                and len(repeated) == 1
                and len(rows) == 1
                and reserved[0]["codigo"] == repeated[0]["codigo"]
            )
        elif scenario == "C3":
            passed = (
                public == [public_refused(), public_refused()]
                and len(rows) == 1
                and rows[0]["codigo"] == seed["codigo"]
            )
        elif scenario == "C4":
            reserved = [item for item in public if item["status"] == "reservada"]
            initial_ok = (
                statuses == ["recusada", "reservada"]
                and len(rows) == 1
                and len(reserved) == 1
            )
            old_codes = all_codes(db_path, strategy)
            if reserved:
                winning_code = reserved[0]["codigo"]
                winning_row = next(
                    (row for row in rows if row["codigo"] == winning_code), None
                )
                if winning_row:
                    cancel_code(db_path, strategy, winning_code)
                    other = apt_b if winning_row["apartamento"] == apt_a else apt_a
                    store = ReservationStore(db_path, strategy, timeout_mode)
                    after = store.gravar_reserva(other, area, data_value)
                    responses.append(
                        {
                            "result": after,
                            "infra_error": store.last_infra_error,
                        }
                    )
                    rows = active_rows(db_path, strategy, area, data_value)
                    passed = (
                        initial_ok
                        and after["status"] == "reservada"
                        and after["codigo"] not in old_codes
                        and len(rows) == 1
                    )
        elif scenario == "C5":
            second_rows = active_rows(
                db_path, strategy, second_area, data_value
            )
            codes = [
                item.get("codigo")
                for item in public
                if item["status"] == "reservada"
            ]
            passed = (
                statuses == ["reservada", "reservada"]
                and len(codes) == 2
                and len(set(codes)) == 2
                and len(rows) == 1
                and len(second_rows) == 1
            )
            rows = rows + second_rows

        detail = {
            "strategy": strategy,
            "mode": mode,
            "scenario": scenario,
            "iteration": index + 1,
            "requests": requests,
            "responses": responses,
            "database_active_rows": rows,
            "passed": passed and not any(
                item.get("infra_error") for item in responses
            ),
        }
        result.observe(passed, detail)
    return result


def make_runner(mode: str) -> PairRunner:
    if mode.startswith("threads"):
        return ThreadRunner()
    if mode.startswith("spawn"):
        return SpawnRunner()
    if mode.startswith("subprocess"):
        return SubprocessRunner()
    if mode.startswith("asyncio"):
        return AsyncRunner()
    raise ValueError(mode)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def cleanup_database(db_path: Path) -> list[str]:
    removed = []
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        if candidate.exists():
            candidate.unlink()
            removed.append(candidate.name)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rodada", type=int, required=True)
    parser.add_argument("--subprocess-worker", action="store_true")
    parser.add_argument("--only-strategy", choices=STRATEGIES)
    parser.add_argument("--only-mode", choices=MODES)
    parser.add_argument("--only-scenario", choices=tuple(CHEAP_SCENARIO_N))
    parser.add_argument("--n-override", type=int)
    parser.add_argument("--debug-failures", action="store_true")
    args = parser.parse_args()
    if args.subprocess_worker:
        return subprocess_worker_main()

    started = time.perf_counter()
    started_wall = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    seeds = load_json(SEED_RESERVAS)
    areas = [item["id"] for item in load_json(SEED_AREAS)]
    apartamentos = [item["numero"] for item in load_json(SEED_APARTAMENTOS)]
    db_path = SPIKE_DIR / (
        f"r{args.rodada}-{os.getpid()}-{uuid.uuid4().hex[:8]}.sqlite3"
    )
    initialize_database(db_path, seeds)

    print(
        json.dumps(
            {
                "event": "run_start",
                "round": args.rodada,
                "started_at": started_wall,
                "python": sys.version.split()[0],
                "sqlite": sqlite3.sqlite_version,
                "journal_mode": "WAL",
                "database": db_path.name,
                "n": {
                    "cheap": CHEAP_SCENARIO_N,
                    "expensive": EXPENSIVE_SCENARIO_N,
                },
                "lock_retries": MAX_LOCK_RETRIES,
                "race_pause_seconds": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )

    summaries: list[dict[str, Any]] = []
    pass_sample: dict[str, Any] | None = None
    fail_sample_e0: dict[str, Any] | None = None
    fail_sample_e3: dict[str, Any] | None = None
    fail_sample_e3_interprocess: dict[str, Any] | None = None
    cleanup_removed: list[str] = []
    try:
        selected_modes = [args.only_mode] if args.only_mode else list(MODES)
        selected_strategies = (
            [args.only_strategy] if args.only_strategy else list(STRATEGIES)
        )
        selected_scenarios = (
            [args.only_scenario]
            if args.only_scenario
            else list(CHEAP_SCENARIO_N)
        )
        if args.n_override is not None:
            for scenario in selected_scenarios:
                CHEAP_SCENARIO_N[scenario] = args.n_override
                EXPENSIVE_SCENARIO_N[scenario] = args.n_override
        for mode in selected_modes:
            runner = make_runner(mode)
            try:
                for strategy in selected_strategies:
                    for scenario in selected_scenarios:
                        scenario_result = run_scenario(
                            str(db_path),
                            runner,
                            strategy,
                            mode,
                            scenario,
                            areas,
                            apartamentos,
                            seeds,
                        )
                        summary = {
                            "event": "summary",
                            "round": args.rodada,
                            "strategy": strategy,
                            "mode": mode,
                            "scenario": scenario,
                            "n": scenario_n(mode, scenario),
                            "failures": scenario_result.failures,
                            "infra_errors": scenario_result.infra_errors,
                        }
                        summaries.append(summary)
                        print(
                            json.dumps(
                                summary, ensure_ascii=False, sort_keys=True
                            ),
                            flush=True,
                        )
                        if (
                            pass_sample is None
                            and strategy == "E1"
                            and scenario == "C1"
                            and scenario_result.first_pass
                        ):
                            pass_sample = scenario_result.first_pass
                        if (
                            fail_sample_e0 is None
                            and strategy == "E0"
                            and scenario_result.first_failure
                        ):
                            fail_sample_e0 = scenario_result.first_failure
                        if strategy == "E3" and scenario_result.first_failure:
                            if fail_sample_e3 is None:
                                fail_sample_e3 = scenario_result.first_failure
                            if (
                                fail_sample_e3_interprocess is None
                                and mode.startswith(("spawn", "subprocess"))
                            ):
                                fail_sample_e3_interprocess = (
                                    scenario_result.first_failure
                                )
                        if args.debug_failures and scenario_result.first_failure:
                            print(
                                json.dumps(
                                    {
                                        "event": "failure_detail",
                                        "detail": scenario_result.first_failure,
                                    },
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                                flush=True,
                            )
            finally:
                runner.close()

        totals = {
            strategy: sum(
                item["failures"]
                for item in summaries
                if item["strategy"] == strategy
            )
            for strategy in STRATEGIES
        }
        infra_totals = {
            strategy: sum(
                item["infra_errors"]
                for item in summaries
                if item["strategy"] == strategy
            )
            for strategy in STRATEGIES
        }
        full_run = not any(
            (
                args.only_strategy,
                args.only_mode,
                args.only_scenario,
                args.n_override is not None,
            )
        )
        e3_interprocess_failures = sum(
            item["failures"]
            for item in summaries
            if item["strategy"] == "E3"
            and item["mode"].startswith(("spawn", "subprocess"))
        )
        e0_interprocess_failures = sum(
            item["failures"]
            for item in summaries
            if item["strategy"] == "E0"
            and item["mode"].startswith(("spawn", "subprocess"))
        )
        expected = full_run and (
            totals["E1"] == 0
            and totals["E2"] == 0
            and totals["E0"] > 0
            and totals["E3"] > 0
            and e3_interprocess_failures > 0
            and infra_totals["E1"] == 0
            and infra_totals["E2"] == 0
        )
        fail_sample = fail_sample_e3_interprocess or fail_sample_e3 or fail_sample_e0
        print(
            json.dumps(
                {"event": "passing_dispute_complete", "detail": pass_sample},
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        print(
            json.dumps(
                {"event": "failing_dispute_e0", "detail": fail_sample_e0},
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        print(
            json.dumps(
                {
                    "event": "failing_dispute_e3",
                    "detail": fail_sample_e3_interprocess or fail_sample_e3,
                    "interprocess": fail_sample_e3_interprocess is not None,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        print(
            json.dumps(
                {"event": "failing_dispute_complete", "detail": fail_sample},
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        print(
            json.dumps(
                {
                    "event": "strategy_totals",
                    "round": args.rodada,
                    "failures": totals,
                    "infra_errors": infra_totals,
                    "e0_interprocess_failures": e0_interprocess_failures,
                    "e3_interprocess_failures": e3_interprocess_failures,
                    "expected_conclusion": expected,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
    finally:
        cleanup_removed = cleanup_database(db_path)

    cleanup_ok = not any(
        Path(f"{db_path}{suffix}").exists() for suffix in ("", "-wal", "-shm")
    )
    elapsed = round(time.perf_counter() - started, 3)
    status = expected and cleanup_ok
    print(
        json.dumps(
            {
                "event": "run_end",
                "round": args.rodada,
                "elapsed_seconds": elapsed,
                "cleanup_removed": cleanup_removed,
                "cleanup_ok": cleanup_ok,
                "experiment_status": "PASS" if status else "FAIL",
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if status else 1


if __name__ == "__main__":
    if "--subprocess-worker" in sys.argv:
        raise SystemExit(subprocess_worker_main())
    multiprocessing.freeze_support()
    raise SystemExit(main())
