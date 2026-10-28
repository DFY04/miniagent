from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional

from miniagent.core.hooks import EventType, EventBus


class Tracer:
    def __init__(self,file_path):
        self.start_time = None
        self.ptah=file_path
        self.trached_id=None
        self._file = open(file_path, 'a', encoding='utf-8')
    def attach(self,bus:EventBus):
        bus.subscribe_all(self.on_event)
    def on_event(self,event:EventType,**kw):
        if(event is EventType.RUN_START):
            self.trached_id = uuid.uuid4().hex[:8]

        #然后不管什么事件都要有一个res
        rec: Dict[str, Any] = {
            'ts': time.time(),
            'type': event.value,          # Enum 的值就是 'run_start' 这种字符串
            'trace_id': self.trached_id,
            'data': kw,
        }
        if(event is EventType.LLM_START):
            self.start_time=time.perf_counter()
        if(event is EventType.LLM_END and self.start_time):
            rec['耗时']=time.perf_counter()-self.start_time
            self.start_time=None

        #吧rec写进文件里面 具体怎么写我不会了

