# This file is a part of Remoulade.
#
# Copyright (C) 2017,2018 CLEARTYPE SRL <bogdan@cleartype.io>
#
# Remoulade is free software; you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at
# your option) any later version.
#
# Remoulade is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser General Public
# License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import atexit
import importlib
import logging
import logging.handlers
import multiprocessing
import os
import random
import signal
import sys
import time
from threading import Thread

from remoulade import ConnectionError, Worker, __version__, get_broker, get_logger

try:
    from .watcher import setup_file_watcher

    HAS_WATCHDOG = True
except ImportError:  # pragma: no cover
    HAS_WATCHDOG = False

#: The exit codes that the master process returns.
RET_OK = 0  # The process terminated successfully.
RET_KILLED = 1  # The process was killed.
RET_IMPORT = 2  # Module import(s) failed or invalid command line argument.
RET_CONNECT = 3  # Broker connection failed during worker startup.
RET_PIDFILE = 4  # PID file points to an existing process or cannot be written to.

#: The size of the logging buffer.
BUFSIZE = 65536

#: The number of available cpus.
CPUS = multiprocessing.cpu_count()

#: The logging format.
LOGFORMAT = "[%(asctime)s] [PID %(process)d] [%(threadName)s] [%(name)s] [%(levelname)s] %(message)s"

#: The logging verbosity levels.
VERBOSITY = {
    0: logging.INFO,
    1: logging.DEBUG,
}

#: Message printed after the help text.
HELP_EPILOG = """\
examples:
  # Run remoulade workers with actors defined in `./some_module.py`.
  $ remoulade some_module


  # Auto-reload remoulade when files in the current directory change.
  $ remoulade --watch . some_module

  # Run remoulade with 1 thread per process.
  $ remoulade --threads 1 some_module

  # Run remoulade with gevent.  Make sure you `pip install gevent` first.
  $ remoulade-gevent --processes 1 --threads 1024 some_module

  # Import extra modules.  Useful when your main module doesn't import
  # all the modules you need.
  $ remoulade some_module some_other_module

  # Listen only to the "foo" and "bar" queues.
  $ remoulade some_module -Q foo bar

  # Write the main process pid to a file.
  $ remoulade some_module --pid-file /tmp/remoulade.pid

  # Write logs to a file.
  $ remoulade some_module --log-file /tmp/remoulade.log
"""


def folder_path(value):
    if not os.path.isdir(value):
        raise argparse.ArgumentError("%r is not a valid directory" % value)
    return os.path.abspath(value)


def parse_arguments():
    parser = argparse.ArgumentParser(
        prog="remoulade",
        description="Run remoulade workers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_EPILOG,
    )
    parser.add_argument(
        "modules", metavar="module", nargs="*",
        help="additional python modules to import",
    )
    parser.add_argument(
        "--processes", "-p", default=CPUS, type=int,
        help="the number of worker processes to run (default: %s)" % CPUS,
    )
    parser.add_argument(
        "--threads", "-t", default=8, type=int,
        help="the number of worker threads per process (default: 8)",
    )
    parser.add_argument(
        "--prefetch-multiplier", default=2, type=int,
        help="""
            the number of messages to prefetch at a time to be multiplied by the number of concurrent processes
            (default:2)
        """,
    )
    parser.add_argument(
        "--path", "-P", default=".", nargs="*", type=str,
        help="the module import path (default: .)"
    )
    parser.add_argument(
        "--queues", "-Q", nargs="*", type=str,
        help="listen to a subset of queues (default: all queues)",
    )
    parser.add_argument(
        "--pid-file", type=str,
        help="write the PID of the master process to a file (default: no pid file)",
    )
    parser.add_argument(
        "--log-file", type=str,
        help="write all logs to a file (default: sys.stderr)",
    )

    if HAS_WATCHDOG:
        parser.add_argument(
            "--watch", type=folder_path, metavar="DIR",
            help=(
                "watch a directory and reload the workers when any source files "
                "change (this feature must only be used during development)"
            )
        )
        parser.add_argument(
            "--watch-use-polling",
            action="store_true",
            help=(
                "poll the filesystem for changes rather than using a "
                "system-dependent filesystem event emitter"
            )
        )

    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--verbose", "-v", action="count", default=0, help="turn on verbose log output")
    return parser.parse_args()


def setup_pidfile(filename):
    try:
        pid = os.getpid()
        with open(filename, "r") as pid_file:
            old_pid = int(pid_file.read().strip())
            # This can happen when reloading the process via SIGHUP.
            if old_pid == pid:
                return pid

        try:
            os.kill(old_pid, 0)
            raise RuntimeError("Remoulade is already running with PID %d." % old_pid)
        except OSError:
            try:
                os.remove(filename)
            except FileNotFoundError:
                pass

    except FileNotFoundError:  # pragma: no cover
        pass

    except ValueError:
        # Abort here to avoid overwriting real files.  Eg. someone
        # accidentally specifies a config file as the pid file.
        raise RuntimeError("PID file contains garbage. Aborting.")

    try:
        with open(filename, "w") as pid_file:
            pid_file.write(str(pid))

        # Change permissions to -rw-r--r--.
        os.chmod(filename, 0o644)
        return pid
    except (FileNotFoundError, PermissionError) as e:
        raise RuntimeError("Failed to write PID file %r. %s." % (e.filename, e.strerror))


def remove_pidfile(filename, logger):
    try:
        logger.debug("Removing PID file %r.", filename)
        os.remove(filename)
    except FileNotFoundError:  # pragma: no cover
        logger.debug("Failed to remove PID file. It's gone.")


def setup_parent_logging(args, *, stream=sys.stderr):
    level = VERBOSITY.get(args.verbose, logging.DEBUG)
    logging.basicConfig(level=level, format=LOGFORMAT, stream=stream)
    return get_logger("remoulade", "MainProcess")


def setup_worker_logging(args, worker_id, log_queue):
    queue_handler = logging.handlers.QueueHandler(log_queue)
    root_logger = logging.getLogger()
    root_logger.setLevel(VERBOSITY.get(args.verbose, logging.DEBUG))
    root_logger.addHandler(queue_handler)
    logging.getLogger("pika").setLevel(logging.CRITICAL)
    return get_logger("remoulade", "WorkerProcess(%s)" % worker_id)


def watch_logs(log_queue):
    watch_logger = get_logger("remoulade", "LogWatcher")
    while True:
        try:
            record = log_queue.get()
            if record is None:
                break

            logger = logging.getLogger(record.name)
            logger.handle(record)
        except Exception:
            watch_logger.exception("Failed to handle log from worker process.")


def worker_process(args, worker_id, log_queue):
    try:
        # Re-seed the random number generator from urandom on
        # supported platforms.  This should make it so that worker
        # processes don't all follow the same sequence.
        random.seed()

        logger = setup_worker_logging(args, worker_id, log_queue)
        broker = get_broker()
        broker.emit_after("process_boot")

        for module in args.modules:
            importlib.import_module(module)

        worker = Worker(broker, queues=args.queues, worker_threads=args.threads)
        worker.start()
    except ImportError:
        logger.exception("Failed to import module.")
        return sys.exit(RET_IMPORT)
    except ConnectionError:
        logger.exception("Broker connection failed.")
        return sys.exit(RET_CONNECT)

    def termhandler(signum, frame):
        nonlocal running
        if running:
            logger.info("Stopping worker process...")
            running = False
        else:
            logger.warning("Killing worker process...")
            return sys.exit(RET_KILLED)

    logger.info("Worker process is ready for action.")
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, termhandler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, termhandler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, termhandler)

    running = True
    while running:
        time.sleep(1)

    worker.stop()
    broker.emit_before("process_stop")
    broker.close()


def main():  # noqa
    args = parse_arguments()
    for path in args.path:
        sys.path.insert(0, path)

    try:
        if args.pid_file:
            setup_pidfile(args.pid_file)
    except RuntimeError as e:
        logger = setup_parent_logging(args, stream=args.log_file or sys.stderr)
        logger.critical(e)
        return RET_PIDFILE

    worker_processes = []
    log_queue = multiprocessing.Queue(-1)
    for worker_id in range(args.processes):
        proc = multiprocessing.Process(
            target=worker_process,
            args=(args, worker_id, log_queue),
            daemon=True,
        )
        proc.start()
        worker_processes.append(proc)

    logger = setup_parent_logging(args, stream=args.log_file or sys.stderr)
    logger.info("remoulade %r is booting up." % __version__)
    if args.pid_file:
        atexit.register(remove_pidfile, args.pid_file, logger)

    running, reload_process = True, False

    # To avoid issues with signal delivery to user threads on
    # platforms such as FreeBSD 10.3, we make the main thread block
    # the signals it expects to handle before spawning the file
    # watcher and log watcher threads so that those threads can
    # inherit the blocking behaviour.
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(
            signal.SIG_BLOCK,
            {signal.SIGINT, signal.SIGTERM, signal.SIGHUP},
        )

    if HAS_WATCHDOG and args.watch:
        file_watcher = setup_file_watcher(args.watch, args.watch_use_polling)

    log_watcher = Thread(target=watch_logs, args=(log_queue,), daemon=True)
    log_watcher.start()

    def stop_worker_processes(signum):
        nonlocal running
        running = False

        for proc in worker_processes:
            try:
                os.kill(proc.pid, signum)
            except OSError:  # pragma: no cover
                if proc.exitcode is None:
                    logger.warning("Failed to send %r to PID %d.", signum.name, proc.pid)

    def sighandler(signum, frame):
        nonlocal reload_process, worker_processes
        reload_process = signum == getattr(signal, "SIGHUP", None)
        if signum == signal.SIGINT:
            signum = signal.SIGTERM

        logger.info("Sending %r to worker processes...", getattr(signum, "name", signum))
        stop_worker_processes(signum)

    # Now that the watcher threads have been started, it should be
    # safe to unblock the signals that were previously blocked.
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(
            signal.SIG_UNBLOCK,
            {signal.SIGINT, signal.SIGTERM, signal.SIGHUP},
        )

    retcode = RET_OK
    signal.signal(signal.SIGINT, sighandler)
    signal.signal(signal.SIGTERM, sighandler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, sighandler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, sighandler)

    # Wait for all worker processes to terminate.  If any of the
    # processes terminates unexpectedly, then shut down the rest as
    # well.
    while any(p.exitcode is None for p in worker_processes):
        for proc in worker_processes:
            proc.join(timeout=1)
            if proc.exitcode is None:
                continue

            if running:  # pragma: no cover
                logger.critical("Worker with PID %r exited unexpectedly (code %r). Shutting down...", proc.pid, proc.exitcode)
                stop_worker_processes(signal.SIGTERM)
                retcode = proc.exitcode
                break

            else:
                retcode = max(retcode, proc.exitcode)

    if HAS_WATCHDOG and args.watch:
        file_watcher.stop()
        file_watcher.join()

    log_queue.put(None)
    log_watcher.join()

    if reload_process:
        if sys.argv[0].endswith("/remoulade/__main__.py"):
            return os.execvp(sys.executable, ["python", "-m", "remoulade", *sys.argv[1:]])
        return os.execvp(sys.argv[0], sys.argv)

    return retcode