#!/usr/bin/env python3
"""Real Linux fault regression. No servers, user media, mocks, or paid providers."""
import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from content_domains import video_compose_editorial as editorial


def identity(pid):
    try:
        fields = pathlib.Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'ppid': int(fields[1]), 'pgid': int(fields[2]),
                'start': int(fields[19]), 'state': fields[0]}
    except FileNotFoundError:
        return None


def tree(root_pid):
    rows = {}
    for entry in pathlib.Path('/proc').iterdir():
        if entry.name.isdigit():
            row = identity(int(entry.name))
            if row:
                rows[row['pid']] = row
    owned = {root_pid}
    while True:
        children = {pid for pid, row in rows.items() if row['ppid'] in owned}
        if children <= owned:
            break
        owned.update(children)
    return [rows[pid] for pid in sorted(owned) if pid in rows]


def still_exists(row):
    current = identity(row['pid'])
    return current is not None and current['start'] == row['start']


def emergency_cleanup(rows):
    # Failure-only cleanup of recorded identities. pidfd prevents a PID reuse
    # race after validation; never pkill or signal a process name/global group.
    for row in reversed(rows):
        try:
            fd = os.pidfd_open(row['pid'])
        except ProcessLookupError:
            continue
        try:
            if still_exists(row):
                signal.pidfd_send_signal(fd, signal.SIGKILL)
        finally:
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=pathlib.Path, required=True)
    args = parser.parse_args()
    if not sys.platform.startswith('linux') or os.geteuid() == 0:
        raise RuntimeError('Run this required test on Linux as a non-root service-like user')
    folder = args.output_dir.absolute()
    folder.mkdir(parents=True, exist_ok=False)
    command, browser = editorial.runtime_command()
    hf_root = pathlib.Path(command[1]).parents[1]
    report = {'uid': os.geteuid(), 'cases': [], 'passed': False}
    sentinel = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(180)'],
                                start_new_session=True)
    sentinel_identity = identity(sentinel.pid)
    try:
        for index, mode in enumerate(['timeout-graceful', 'timeout-forced', 'timeout-forced',
                                      'exit-success', 'exit-failure']):
            case_dir = folder / f'{index}-{mode}'
            case_dir.mkdir()
            observed = []
            observer_errors = []
            record = {}
            stop = threading.Event()

            def observe():
                try:
                    while not stop.wait(.02):
                        launch = case_dir / 'launch.json'
                        if not launch.exists():
                            continue
                        try:
                            record.update(json.loads(launch.read_text()))
                        except json.JSONDecodeError:
                            continue
                        # Register fixture-owned roots first so a deliberately
                        # broken/old adapter's Chrome can still be cleaned up.
                        owned = {row['pid']: row for row in tree(record['node'])}
                        for row in tree(record['grandchild']):
                            owned[row['pid']] = row
                        observed.extend(owned.values())
                        # Fail safely if someone reruns this against an old
                        # adapter: its Node parent may be this test process,
                        # whose tree includes the unrelated sentinel.
                        assert record['supervisor'] != os.getpid(), 'Expected isolated supervisor'
                        supervisor_args = pathlib.Path(
                            f'/proc/{record["supervisor"]}/cmdline').read_bytes().split(b'\0')
                        assert any(value.endswith(b'/editorial_process_supervisor.py')
                                   for value in supervisor_args), supervisor_args
                        for row in tree(record['supervisor']):
                            if row['pid'] not in owned:
                                observed.append(row)
                        (case_dir / 'observed').touch()
                        return
                except Exception as error:
                    observer_errors.append(repr(error))

            observer = threading.Thread(target=observe)
            observer.start()
            started = time.monotonic()
            outcome = 'success'
            environment = {**os.environ, 'TMPDIR': str(case_dir), 'TEMP': str(case_dir),
                           'TMP': str(case_dir), 'DO_NOT_TRACK': '1'}
            try:
                editorial._run_owned([command[0], str(ROOT / 'scripts/editorial_process_fixture.mjs'),
                    str(hf_root), browser, str(case_dir), mode], case_dir, environment, 8)
            except subprocess.TimeoutExpired:
                outcome = 'timeout'
            except subprocess.CalledProcessError as error:
                outcome = 'failure'
                (case_dir / 'stderr.txt').write_bytes(error.stderr or b'')
            finally:
                stop.set()
                observer.join(timeout=2)
            survivors = [row for row in observed if still_exists(row)]
            result = {'mode': mode, 'outcome': outcome, 'seconds': time.monotonic() - started,
                      'launch': record, 'observed': observed, 'survivors': survivors,
                      'observer_errors': observer_errors,
                      'sentinel_alive': still_exists(sentinel_identity)}
            report['cases'].append(result)
            (folder / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            try:
                assert not observer.is_alive() and not observer_errors, result
                assert record and observed, 'Fixture failed to launch real browser and process tree'
                assert record['versions'] == {'hyperframes': '0.8.33', 'puppeteer-core': '25.10.0',
                                                '@puppeteer/browsers': '3.2.2'}, record
                by_pid = {row['pid']: row for row in observed}
                assert sentinel.pid not in by_pid, 'Unrelated sentinel entered owned tree'
                for name in ['node', 'supervisor', 'chrome', 'ordinary', 'detached', 'ffmpeg', 'grandchild']:
                    assert record[name] in by_pid, (name, result)
                assert by_pid[record['chrome']]['pgid'] == record['chrome'], result
                assert by_pid[record['chrome']]['pgid'] != by_pid[record['node']]['pgid'], result
                assert by_pid[record['grandchild']]['ppid'] == record['supervisor'], result
                assert '--remote-debugging-port=0' in record['chromeArgs'], record
                assert '--remote-debugging-pipe' not in record['chromeArgs'], record
                expected = 'timeout' if mode.startswith('timeout') else (
                    'failure' if mode == 'exit-failure' else 'success')
                assert outcome == expected, result
                assert not survivors, result
                assert result['sentinel_alive'] and sentinel.poll() is None, result
                assert result['seconds'] < 8 + editorial._CLEANUP_BUDGET, result
            finally:
                if survivors:
                    emergency_cleanup(survivors)
        # Same API runner remains usable after consecutive real timeouts/failures.
        recovered = editorial._run_owned([command[0], '-e', 'console.log("recovered")'],
                                         folder, os.environ.copy(), 5)
        assert recovered.stdout.strip() == b'recovered'
        report['subsequent_task'] = 'passed'
        report['passed'] = True
        (folder / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report, indent=2))
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=5)


if __name__ == '__main__':
    main()
