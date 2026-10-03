"""会话持有的后台筛选任务。工作线程只处理数据，不调用 Streamlit。"""
from __future__ import annotations
from threading import Lock, Thread
import time
import uuid

import data
from explain import verify_explanations
from schema import ScreeningRequest
from screen import run_screen


class ScreeningJob:
    def __init__(self, request, *, client_factory=None, runner=None):
        self.id = uuid.uuid4().hex
        self.request_json = request.model_dump_json()
        self.query = request.query
        self.started = time.perf_counter()
        self._lock = Lock()
        self._state = dict(status='running', message='正在获取最新股票池、行情与财报…',
                           done=0, total=0, eta=None, context=None, error=None)
        self._factory = client_factory or data.DataClient
        self._runner = runner or run_screen
        self._thread = Thread(target=self._run, name='screen-'+self.id[:8], daemon=True)

    def start(self):
        self._thread.start()
        return self

    def update(self, **values):
        with self._lock:
            self._state.update(values)

    def snapshot(self):
        with self._lock:
            return dict(self._state, id=self.id, query=self.query,
                        elapsed=time.perf_counter()-self.started)

    def _run(self):
        try:
            request = ScreeningRequest.model_validate_json(self.request_json)
            client = self._factory(force_refresh=True, logger=None)
            universe = client.build_universe()
            client.pinned_universe = universe
            self.update(message='在线数据已读取，正在应用条件与计算走势…')
            kline_started = time.perf_counter()

            def progress(done, total):
                eta = (time.perf_counter()-kline_started)/max(done, 1)*(total-done)/60
                self.update(done=done, total=total, eta=eta,
                            message=f'正在计算走势 {done}/{total}')

            result = self._runner(request, client, progress)
            verify_explanations(result.rows)
            context = dict(result=result, universe=universe,
                           elapsed=time.perf_counter()-self.started,
                           requests=client.stats.http_requests, cache_hits=client.stats.cache_hits)
            self.update(status='succeeded', message='筛选完成', context=context)
        except Exception as exc:
            self.update(status='failed', message='本轮筛选未完成',
                        error=f'{type(exc).__name__}: {exc}')


def start_screening(request):
    return ScreeningJob(request).start()
