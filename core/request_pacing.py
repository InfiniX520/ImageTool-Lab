"""A shared, cancellable start interval. No queue, background owner or timer."""
import math
import threading
import time


class RequestPacer:
    def __init__(self, interval=.5, *, clock=time.monotonic, sleep=time.sleep):
        if type(interval) not in (int,float) or not math.isfinite(interval) or not 0<=interval<=60:
            raise ValueError('Invalid request interval')
        self.interval,self.clock,self.sleep=interval,clock,sleep
        self._next=0.0
        self._guard=threading.Lock()

    def __call__(self,stop=None):
        while True:
            if stop is not None and stop.is_set():
                return False
            with self._guard:
                now=self.clock()
                remaining=self._next-now
                if remaining<=0:
                    self._next=now+self.interval
                    return True
            # A cancelled waiter never reserves a future slot. Fixed slices
            # also support the coordinator's composite cancellation tokens.
            self.sleep(min(.05,remaining))
