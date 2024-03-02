#!/usr/bin/python3
# coding=utf-8
# pylint: disable=C0116,C0302

#   Copyright 2023 getcarrier.io
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""
    Task node

    Allows to start, register, query tasks and workers

    Uses existing EventNode as a transport
"""

import os
import gzip
import time
import uuid
import queue
import pickle
import datetime
import threading
import functools
import traceback
import multiprocessing
import multiprocessing.connection

from arbiter import log

from .eventnode.tools import make_event_node


class TaskNode:  # pylint: disable=R0902,R0904
    """ Task node - start, register, query tasks and workers """

    def __init__(  # pylint: disable=R0913,R0914
            self, event_node,
            pool=None, task_limit=None, ident_prefix="",
            multiprocessing_context="spawn", kill_on_stop=False,
            task_retention_period=3600, housekeeping_interval=60,
            start_max_wait=3, query_wait=3,
            watcher_max_wait=3, stop_node_task_wait=3, result_max_wait=3,
            tmp_path="/tmp/tasknode", result_transport="memory",
    ):
        self.event_node = event_node
        self.event_node_was_started = False
        #
        self.ident_prefix = ident_prefix
        self.ident = None
        self.pool = pool
        #
        self.sync_queues = {}
        self.task_registry = {}
        self.running_tasks = {}
        #
        self.global_pool_state = {}
        self.global_task_state = {}
        #
        self.have_running_tasks = threading.Event()
        self.known_task_ids = set()
        self.state_events = {}
        self.task_status_subscribers = []
        #
        self.multiprocessing_context = multiprocessing_context
        self.kill_on_stop = kill_on_stop
        self.task_limit = task_limit
        self.task_retention_period = task_retention_period
        #
        self.tmp_path = tmp_path
        self.result_transport = result_transport
        #
        self.housekeeping_interval = housekeeping_interval
        self.start_max_wait = start_max_wait
        self.query_wait = query_wait
        self.watcher_max_wait = watcher_max_wait
        self.stop_node_task_wait = stop_node_task_wait
        self.result_max_wait = result_max_wait
        #
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.started = False

    #
    # Node start and stop
    #

    def start(self, block=False):
        """ Start task node """
        if self.started:
            return
        #
        self.stop_event.clear()
        #
        if not self.event_node.started:
            self.event_node.start()
            self.event_node_was_started = True
        #
        self.ident = f'{self.ident_prefix}{str(uuid.uuid4())}'
        #
        if self.result_transport == "files":
            os.makedirs(self.tmp_path, exist_ok=True)
            self.event_node.subscribe("task_result_payload", self.on_result_payload)
        #
        self.event_node.subscribe("task_node_announce", self.on_node_announce)
        self.event_node.subscribe("task_node_withhold", self.on_node_withhold)
        #
        self.event_node.subscribe("task_start_query", self.on_start_query)
        self.event_node.subscribe("task_start_candidate", self.on_sync_reply)
        self.event_node.subscribe("task_start_request", self.on_start_request)
        self.event_node.subscribe("task_start_ack", self.on_sync_reply)
        #
        self.event_node.subscribe("task_stop_request", self.on_stop_request)
        self.event_node.subscribe("task_state_announce", self.on_state_announce)
        #
        self.event_node.subscribe("task_state_query", self.on_state_query)
        self.event_node.subscribe("task_state_reply", self.on_state_reply)
        self.event_node.subscribe("task_pool_query", self.on_pool_query)
        self.event_node.subscribe("task_pool_reply", self.on_pool_reply)
        #
        TaskNodeWatcher(self).start()
        TaskNodeHousekeeper(self).start()
        #
        self.event_node.emit(
            "task_node_announce",
            {
                "ident": self.ident,
                "pool": self.pool,
                "task_limit": self.task_limit,
                "running_tasks": 0,
            }
        )
        #
        self.started = True
        #
        if block:
            self.stop_event.wait()

    def stop(self, block=True):
        """ Stop task node """
        self.event_node.unsubscribe("task_node_announce", self.on_node_announce)
        self.event_node.unsubscribe("task_node_withhold", self.on_node_withhold)
        #
        self.event_node.emit(
            "task_node_withhold",
            {
                "ident": self.ident,
            }
        )
        #
        self.event_node.unsubscribe("task_start_query", self.on_start_query)
        self.event_node.unsubscribe("task_start_candidate", self.on_sync_reply)
        self.event_node.unsubscribe("task_start_request", self.on_start_request)
        self.event_node.unsubscribe("task_start_ack", self.on_sync_reply)
        #
        for task_id in list(self.running_tasks):
            self.stop_task(task_id)
            if block:
                self.wait_for_task(task_id, self.stop_node_task_wait)
        #
        self.event_node.unsubscribe("task_stop_request", self.on_stop_request)
        self.event_node.unsubscribe("task_state_announce", self.on_state_announce)
        #
        if self.result_transport == "files":
            self.event_node.unsubscribe("task_result_payload", self.on_result_payload)
        #
        self.event_node.unsubscribe("task_state_query", self.on_state_query)
        self.event_node.unsubscribe("task_state_reply", self.on_state_reply)
        self.event_node.unsubscribe("task_pool_query", self.on_pool_query)
        self.event_node.unsubscribe("task_pool_reply", self.on_pool_reply)
        #
        while self.task_status_subscribers:
            subscriber = self.task_status_subscribers.pop()
            self.event_node.unsubscribe("task_status_change", subscriber)
        #
        if self.event_node_was_started:
            self.event_node.stop()
        #
        self.started = False
        self.stop_event.set()

    #
    # Task registration
    #

    def register_task(self, func, name=None):
        """ Register task function """
        if name is None:
            name = self.get_callable_name(func)
        #
        with self.lock:
            self.task_registry[name] = func

    def unregister_task(self, func=None, name=None):
        """ Unregister task function """
        if name is None and func is None:
            raise ValueError("Missing name or func")
        #
        if name is None:
            name = self.get_callable_name(func)
        #
        with self.lock:
            if name in self.task_registry:
                self.task_registry.pop(name)

    #
    # Task start and stop
    #

    def start_task(self, name, args=None, kwargs=None, pool=None, meta=None):  # pylint: disable=R0913
        """ Start task execution """
        if meta is not None and not isinstance(meta, dict):
            raise ValueError("Meta must be None or dict")
        #
        task_id = self.generate_task_id()
        #
        self.event_node.emit(
            "task_state_announce",
            {
                "task_id": task_id,
                "requestor": self.ident,
                "runner": None,
                "status": "pending",
                "result": None,
                "meta": meta,
            }
        )
        #
        self.event_node.emit(
            "task_status_change",
            {
                "task_id": task_id,
                "status": "pending",
            }
        )
        #
        query_queue = f'task_start_query_{task_id}'
        ack_queue = f'task_start_ack_{task_id}'
        #
        with self.lock:
            self.sync_queues[query_queue] = queue.Queue()
            self.sync_queues[ack_queue] = queue.Queue()
        #
        self.event_node.emit(
            "task_start_query",
            {
                "name": name,
                "pool": pool,
                "task_id": task_id,
                "requestor": self.ident,
                "sync_queue": query_queue,
            }
        )
        #
        try:
            while True:
                try:
                    candidate = self.sync_queues[query_queue].get(timeout=self.start_max_wait)
                    #
                    self.event_node.emit(
                        "task_start_request",
                        {
                            "name": name,
                            "meta": meta,
                            "args": args,
                            "kwargs": kwargs,
                            "pool": pool,
                            "task_id": task_id,
                            "runner": candidate.get("ident"),
                            "requestor": self.ident,
                            "sync_queue": ack_queue,
                        }
                    )
                    #
                    try:
                        self.sync_queues[ack_queue].get(timeout=self.start_max_wait)
                    except:  # pylint: disable=W0702
                        continue  # try next candidate if present
                    #
                    return task_id
                except:  # pylint: disable=W0702
                    self.event_node.emit(
                        "task_state_announce",
                        {
                            "task_id": task_id,
                            "requestor": self.ident,
                            "runner": None,
                            "status": "stopped",
                            "result": None,
                            "meta": meta,
                        }
                    )
                    #
                    self.event_node.emit(
                        "task_status_change",
                        {
                            "task_id": task_id,
                            "status": "stopped",
                        }
                    )
                    #
                    return None
        finally:
            with self.lock:
                self.sync_queues.pop(query_queue)
                self.sync_queues.pop(ack_queue)

    def stop_task(self, task_id):
        """ Stop running task """
        self.event_node.emit(
            "task_stop_request",
            {
                "task_id": task_id,
                "requestor": self.ident,
            }
        )

    #
    # Wait for task / join on task
    #

    def wait_for_task(self, task_id, timeout=None):
        """ Wait for task to stop """
        if task_id not in self.state_events:
            self.query_task_state(task_id)
        #
        if task_id not in self.state_events:
            raise RuntimeError("Unknown task")
        #
        self.state_events[task_id]["event"].wait(timeout)

    def join_task(self, task_id, timeout=None):
        """ Wait for task to stop and get task result """
        self.wait_for_task(task_id, timeout)
        return self.get_task_result(task_id)

    #
    # Task status, meta and result
    #

    def get_task_status(self, task_id):
        """ Get task status """
        if task_id not in self.global_task_state:
            self.query_task_state(task_id)
        #
        if task_id not in self.global_task_state:
            raise RuntimeError("Unknown task")
        #
        return self.global_task_state[task_id].get("status", "unknown")

    def get_task_meta(self, task_id):
        """ Get task meta """
        if task_id not in self.global_task_state:
            self.query_task_state(task_id)
        #
        if task_id not in self.global_task_state:
            raise RuntimeError("Unknown task")
        #
        meta = self.global_task_state[task_id].get("meta", None)
        if meta is None:
            meta = {}
        #
        return meta.copy()

    def get_task_result(self, task_id):
        """ Get task result """
        if task_id not in self.global_task_state:
            self.query_task_state(task_id)
        #
        if task_id not in self.global_task_state:
            raise RuntimeError("Unknown task")
        #
        result = self.global_task_state[task_id].get("result", None)
        #
        if result is None:
            return ...  # invalid result or task is still running
        #
        result = pickle.loads(gzip.decompress(result))
        #
        if "return" in result:
            return result["return"]
        #
        if "raise" in result:
            raise Exception("\n".join(["", result["raise"]]))  # pylint: disable=W0719
        #
        return ...  # invalid result

    def subscribe_to_task_statuses(self, func):
        """ Subscribe to task status changes """
        self.event_node.subscribe("task_status_change", func)
        with self.lock:
            self.task_status_subscribers.append(func)

    #
    # Node network queries
    #

    def query_task_state(self, task_id=None):
        """ Sync info from other nodes """
        self.event_node.emit(
            "task_state_query",
            {
                "task_id": task_id,
                "requestor": self.ident,
            }
        )
        #
        time.sleep(self.query_wait)

    def query_pool_state(self, pool=None):
        """ Sync info from other nodes """
        self.event_node.emit(
            "task_pool_query",
            {
                "pool": pool,
                "requestor": self.ident,
            }
        )
        #
        time.sleep(self.query_wait)

    def count_free_workers(self, pool=None):
        """ Get task limit (how many can we start now) """
        self.query_pool_state(pool)
        #
        if pool not in self.global_pool_state:
            return 0
        #
        free = 0
        #
        for data in self.global_pool_state[pool].values():
            if data["task_limit"] is None:
                return ...  # unlimited
            #
            free += (data["task_limit"] - data["running_tasks"])
        #
        return free

    #
    # Event handlers
    #

    def on_node_announce(self, event_name, event_payload):
        _ = event_name
        #
        if "for_requestor" in event_payload and event_payload.get("for_requestor") != self.ident:
            return
        #
        if "ident" not in event_payload:
            return
        #
        ident = event_payload.get("ident")
        pool = event_payload.get("pool", None)
        #
        with self.lock:
            if pool not in self.global_pool_state:
                self.global_pool_state[pool] = {}
            #
            self.global_pool_state[pool][ident] = event_payload.copy()

    def on_node_withhold(self, event_name, event_payload):
        _ = event_name
        #
        if "ident" not in event_payload:
            return
        #
        ident = event_payload.get("ident")
        #
        with self.lock:
            for _, nodes in self.global_pool_state.items():
                nodes.pop(ident, None)

    def on_stop_request(self, event_name, event_payload):
        _ = event_name
        #
        if "task_id" not in event_payload:
            return
        #
        task_id = event_payload.get("task_id")
        #
        if task_id not in self.running_tasks:
            return
        #
        with self.lock:
            data = self.running_tasks.get(task_id, {})
            process = data.get("process", None)
        #
        if process is not None:
            if self.kill_on_stop:
                process.kill()
            else:
                process.terminate()

    def on_state_announce(self, event_name, event_payload):
        _ = event_name
        #
        if "for_requestor" in event_payload and event_payload.get("for_requestor") != self.ident:
            return
        #
        if "task_id" not in event_payload:
            return
        #
        task_id = event_payload.get("task_id")
        task_status = event_payload.get("status", "unknown")
        #
        with self.lock:
            self.global_task_state[task_id] = event_payload.copy()
            self.known_task_ids.add(task_id)
            #
            if task_id not in self.state_events:
                self.state_events[task_id] = {
                    "event": threading.Event(),
                }
            #
            self.state_events[task_id]["timestamp"] = datetime.datetime.now()
            #
            if task_status == "stopped":
                self.state_events[task_id]["event"].set()

    def on_result_payload(self, event_name, event_payload):
        _ = event_name
        #
        task_id = event_payload.get("task_id")
        payload = event_payload.get("payload")
        #
        with self.lock:
            if task_id not in self.running_tasks:
                return
            #
            self.running_tasks[task_id]["result"] = payload

    def on_state_query(self, event_name, event_payload):
        _ = event_name
        #
        if event_payload.get("requestor", None) == self.ident:
            return
        #
        if event_payload.get("task_id", None) is not None:
            task_id = event_payload.get("task_id")
            #
            if task_id not in self.global_task_state:
                return
            #
            task_state = self.global_task_state[task_id].copy()
            task_state["for_requestor"] = event_payload.get("requestor", None)
            #
            self.event_node.emit(
                "task_state_announce",
                task_state
            )
        else:
            self.event_node.emit(
                "task_state_reply",
                {
                    "for_requestor": event_payload.get("requestor", None),
                    "global_task_state": self.global_task_state,
                }
            )

    def on_state_reply(self, event_name, event_payload):
        _ = event_name
        #
        if event_payload.get("for_requestor", None) != self.ident:
            return
        #
        if "global_task_state" not in event_payload:
            return
        #
        global_task_state = event_payload.get("global_task_state")
        #
        with self.lock:
            for task_id in list(self.global_task_state):
                if task_id in self.running_tasks:
                    global_task_state.pop(task_id, None)
                else:
                    self.global_task_state.pop(task_id, None)
            #
            self.global_task_state.update(global_task_state)

    def on_pool_query(self, event_name, event_payload):
        _ = event_name
        #
        if event_payload.get("requestor", None) == self.ident:
            return
        #
        if event_payload.get("pool", None) is not None:
            pool = event_payload.get("pool")
            #
            if pool not in self.global_pool_state:
                return
            #
            global_pool_state = {
                pool: self.global_pool_state[pool]
            }
        else:
            global_pool_state = self.global_pool_state
        #
        self.event_node.emit(
            "task_pool_reply",
            {
                "for_requestor": event_payload.get("requestor", None),
                "global_pool_state": global_pool_state,
            }
        )

    def on_pool_reply(self, event_name, event_payload):
        _ = event_name
        #
        if event_payload.get("for_requestor", None) != self.ident:
            return
        #
        if "global_pool_state" not in event_payload:
            return
        #
        global_pool_state = event_payload.get("global_pool_state")
        #
        with self.lock:
            for pool, state in global_pool_state.items():
                if pool not in self.global_pool_state:
                    self.global_pool_state[pool] = state
                    continue
                #
                for ident in list(self.global_pool_state[pool]):
                    if ident == self.ident:
                        continue
                    #
                    self.global_pool_state[pool].pop(ident, None)
                #
                state.pop(self.ident, None)
                self.global_pool_state[pool].update(state)

    def on_sync_reply(self, event_name, event_payload):
        _ = event_name
        #
        if event_payload.get("for_requestor", None) != self.ident:
            return
        #
        if event_payload.get("sync_queue", None) not in self.sync_queues:
            return
        #
        self.sync_queues[event_payload.get("sync_queue")].put(event_payload.copy())

    def on_start_query(self, event_name, event_payload):
        _ = event_name
        #
        if event_payload.get("name", None) not in self.task_registry:
            return
        #
        if event_payload.get("pool", None) != self.pool:
            return
        #
        if self.task_limit is not None and len(self.running_tasks) >= self.task_limit:
            return
        #
        self.event_node.emit(
            "task_start_candidate",
            {
                "ident": self.ident,
                "for_requestor": event_payload.get("requestor", None),
                "sync_queue": event_payload.get("sync_queue", None),
            }
        )

    def on_start_request(self, event_name, event_payload):
        _ = event_name
        #
        if event_payload.get("runner", None) != self.ident:
            return
        #
        if event_payload.get("name", None) not in self.task_registry:
            return
        #
        if event_payload.get("pool", None) != self.pool:
            return
        #
        if self.task_limit is not None and len(self.running_tasks) >= self.task_limit:
            return
        #
        self.event_node.emit(
            "task_start_ack",
            {
                "for_requestor": event_payload.get("requestor", None),
                "sync_queue": event_payload.get("sync_queue", None),
            }
        )
        #
        self.event_node.emit(
            "task_state_announce",
            {
                "task_id": event_payload.get("task_id", None),
                "requestor": event_payload.get("requestor", None),
                "runner": self.ident,
                "status": "running",
                "result": None,
                "meta": event_payload.get("meta", None),
            }
        )
        #
        self.event_node.emit(
            "task_status_change",
            {
                "task_id": event_payload.get("task_id", None),
                "status": "running",
            }
        )
        #
        self.execute_local_task(
            event_payload.get("task_id", None),
            event_payload.get("name", None),
            event_payload.get("meta", None),
            event_payload.get("args", None),
            event_payload.get("kwargs", None)
        )

    #
    # Tools
    #

    def generate_task_id(self):
        """ Get 'mostly' safe new task_id """
        with self.lock:
            while True:
                task_id = str(uuid.uuid4())
                #
                if task_id in self.known_task_ids:
                    continue
                #
                self.known_task_ids.add(task_id)
                break
        #
        return task_id

    def execute_local_task(self, task_id, name, meta, args=None, kwargs=None):  # pylint: disable=R0913
        """ Start task from task registry """
        if name not in self.task_registry:
            raise RuntimeError("Task not found")
        #
        if meta is None:
            meta = {}
        if args is None:
            args = ()
        if kwargs is None:
            kwargs = {}
        #
        result = None
        if self.result_transport == "files":
            result_config = self.tmp_path
        elif self.result_transport == "events":
            result_config = self.event_node.clone_config.copy()
        elif self.result_transport == "memory":
            result_config = multiprocessing.Queue()
            result = result_config
        else:
            raise RuntimeError(f"Invalid result transport: {self.result_transport}")
        #
        process = multiprocessing.get_context(self.multiprocessing_context).Process(
            target=self.executor,
            args=(),
            kwargs={
                "target": self.task_registry[name],
                "task_id": task_id,
                "meta": meta,
                "args": args,
                "kwargs": kwargs,
                "result_transport": self.result_transport,
                "result_config": result_config,
                "multiprocessing_context": self.multiprocessing_context,
            },
            daemon=False,
        )
        process.start()
        #
        with self.lock:
            self.running_tasks[task_id] = {
                "process": process,
                "result": result,
            }
            self.have_running_tasks.set()
        #
        self.event_node.emit(
            "task_node_announce",
            {
                "ident": self.ident,
                "pool": self.pool,
                "task_limit": self.task_limit,
                "running_tasks": len(self.running_tasks),
            }
        )

    @staticmethod
    def executor(
            target, task_id, meta, args, kwargs,
            result_transport, result_config, multiprocessing_context,
    ):  # pylint: disable=R0913,R0914
        """ Task executor """
        try:
            import setproctitle  # pylint: disable=C0415,E0401
            setproctitle.setproctitle(f'tasknode_task {task_id}')
            #
            if multiprocessing_context == "fork":
                # After fork
                import ssl  # pylint: disable=C0415
                ssl.RAND_bytes(1)
                import signal  # pylint: disable=C0415
                signal.signal(signal.SIGTERM, lambda *x, **y: os._exit(0))  # pylint: disable=W0212
                # Also need to think about gevent? logging? base pylon re-init here?
            #
            import sys  # pylint: disable=C0415
            import types  # pylint: disable=C0415
            sys.modules["tasknode_task"] = types.ModuleType("tasknode_task")
            sys.modules["tasknode_task"].__path__ = []
            setattr(sys.modules["tasknode_task"], "id", task_id)
            setattr(sys.modules["tasknode_task"], "meta", meta.copy())
            #
            try:
                output = target(*args, **kwargs)
                data = {"return": output}
            except:  # pylint: disable=W0702
                error = traceback.format_exc()
                data = {"raise": error}
            #
            result = gzip.compress(pickle.dumps(
                data, protocol=pickle.HIGHEST_PROTOCOL
            ))
            #
            if result_transport == "files":
                with open(os.path.join(result_config, f'{task_id}.bin'), "wb") as file:
                    file.write(result)
            #
            elif result_transport == "events":
                result_event_node = make_event_node(config=result_config)
                result_event_node.start(emit_only=True)
                result_event_node.emit("task_result_payload", {
                    "task_id": task_id,
                    "payload": result,
                })
                result_event_node.stop()
            #
            elif result_transport == "memory":
                result_config.put(result)
            #
            else:
                raise RuntimeError(f"Invalid result transport: {result_transport}")
        except:  # pylint: disable=W0702
            log.exception("Task execution failed")
            raise

    def get_callable_name(self, func):
        """ Get callable name """
        if hasattr(func, "__name__"):
            return func.__name__
        if isinstance(func, functools.partial):
            return self.get_callable_name(func.func)
        raise ValueError("Cannot guess callable name")


class TaskNodeWatcher(threading.Thread):  # pylint: disable=R0903
    """ Watch running tasks """

    def __init__(self, node):
        super().__init__(daemon=True)
        self.node = node

    def run(self):
        """ Run watcher thread """
        #
        while not self.node.stop_event.is_set():
            try:
                self._watch_stopped_tasks()
            except:  # pylint: disable=W0702
                log.exception("Exception in watcher thread, continuing")

    def _watch_stopped_tasks(self):  # pylint: disable=R0912,R0915
        #
        # Wait until we have tasks to watch
        #
        watcher_max_wait = self.node.watcher_max_wait
        self.node.have_running_tasks.wait(watcher_max_wait)
        #
        if self.node.result_transport == "events":
            #
            # Check stopped tasks with missing results
            #
            with self.node.lock:
                done_tasks = []
                #
                for task_id, data in self.node.running_tasks.items():
                    if data["process"] is not None:
                        continue
                    #
                    if data["result"] is not None:
                        done_task = (task_id, data["result"])
                        done_tasks.append(done_task)
                        continue
                    #
                    age = (datetime.datetime.now() - data["timestamp"]).total_seconds()
                    if age < self.node.result_max_wait:
                        continue
                    #
                    done_task = (task_id, data["result"])
                    done_tasks.append(done_task)
            #
            # Announce late and expired
            #
            for task_id, result in done_tasks:
                self._announce_task_stopped(task_id, result)
        #
        # Collect sentinels of running tasks
        #
        with self.node.lock:
            sentinel_map = {}
            #
            for task_id, data in self.node.running_tasks.items():
                if data["process"] is None:
                    continue  # task is stopped, awaiting result
                sentinel_map[data["process"].sentinel] = task_id
        #
        # Wait for tasks to stop
        #
        ready_sentinels = multiprocessing.connection.wait(
            list(sentinel_map), timeout=watcher_max_wait
        )
        #
        # Process newly stopped tasks
        #
        for sentinel in ready_sentinels:
            task_id = sentinel_map[sentinel]
            task_data = self.node.running_tasks[task_id]
            #
            try:
                task_data["process"].join(1)
                task_data["process"].close()
            except:  # pylint: disable=W0702
                log.exception("Failed to close process, continuing")
            finally:
                task_data["process"] = None
            #
            if self.node.result_transport == "files":
                try:
                    result_path = os.path.join(self.node.tmp_path, f'{task_id}.bin')
                    with open(result_path, "rb") as file:
                        task_data["result"] = file.read()
                    os.remove(result_path)
                except:  # pylint: disable=W0702
                    log.exception("Failed to load/remove result, continuing")
            #
            elif self.node.result_transport == "events" and task_data["result"] is None:
                # Result event is not processed (or process crashed badly)
                task_data["timestamp"] = datetime.datetime.now()
                continue
            elif self.node.result_transport == "memory":
                try:
                    result = task_data["result"].get_nowait()
                except:  # pylint: disable=W0702
                    result = None
                #
                try:
                    task_data["result"].close()
                except:  # pylint: disable=W0702
                    log.exception("Failed to close result, continuing")
                #
                task_data["result"] = result
            #
            self._announce_task_stopped(task_id, task_data["result"])

    def _announce_task_stopped(self, task_id, result):
        task_state = self.node.global_task_state[task_id].copy()
        #
        task_state["status"] = "stopped"
        task_state["result"] = result
        #
        with self.node.lock:
            self.node.running_tasks.pop(task_id)
            if not self.node.running_tasks:
                self.node.have_running_tasks.clear()
        #
        self.node.event_node.emit(
            "task_node_announce",
            {
                "ident": self.node.ident,
                "pool": self.node.pool,
                "task_limit": self.node.task_limit,
                "running_tasks": len(self.node.running_tasks),
            }
        )
        #
        self.node.event_node.emit(
            "task_state_announce",
            task_state
        )
        #
        self.node.event_node.emit(
            "task_status_change",
            {
                "task_id": task_id,
                "status": "stopped",
            }
        )


class TaskNodeHousekeeper(threading.Thread):  # pylint: disable=R0903
    """ Perform cleanup """

    def __init__(self, node):
        super().__init__(daemon=True)
        self.node = node

    def run(self):
        """ Run housekeeper thread """
        while not self.node.stop_event.is_set():
            time.sleep(self.node.housekeeping_interval)
            #
            with self.node.lock:
                for task_id in list(self.node.state_events):
                    data = self.node.state_events[task_id]
                    #
                    if not data["event"].is_set():
                        continue
                    #
                    age = (datetime.datetime.now() - data["timestamp"]).total_seconds()
                    #
                    if age < self.node.task_retention_period:
                        continue
                    #
                    self.node.state_events.pop(task_id, None)
                    self.node.global_task_state.pop(task_id, None)
                    self.node.known_task_ids.discard(task_id)
                    #
                    self.node.event_node.emit(
                        "task_status_change",
                        {
                            "task_id": task_id,
                            "status": "pruned",
                        }
                    )
