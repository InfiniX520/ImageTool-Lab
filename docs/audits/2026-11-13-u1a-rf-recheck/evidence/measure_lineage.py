import os, sqlite3, sys, time, json
from pathlib import Path
root = Path(os.environ['PIXIVKEEP_AUDIT_REPO']).resolve()
sys.path[:0] = [str(root), str(root / 'tests'), str(Path(__file__).parent)]
import test_u1a_recovery as h
from test_u1a_rf_recovery import clone_delivery
from test_rf_recheck import handover
from core.delivery_lineage import resolve_successor
out = Path(os.environ['PIXIVKEEP_AUDIT_OUTPUT']).resolve()
out.mkdir(exist_ok=False)
results = []
for n in (8, 16, 24, 32):
    sample = out / str(n)
    sample.mkdir()
    with h.LibraryRepository(sample / 'ledger.sqlite3') as repo:
        _, hold, expected = handover(repo, sample)
        with sqlite3.connect(repo.database_path) as conn:
            conn.row_factory = sqlite3.Row
            for i in range(n):
                clone_delivery(conn, hold, f'{i+1:032x}', generation=20+i)
            conn.commit()
            row = conn.execute('SELECT * FROM deliveries WHERE delivery_id=?', (hold,)).fetchone()
            count = [0]
            def trace(sql): count[0] += 1
            start = time.perf_counter()
            conn.set_trace_callback(trace)
            conn.set_progress_handler(lambda: int(time.perf_counter() - start > 10), 5000)
            status = 'resolved'
            try:
                result = resolve_successor(conn, row)
                assert result and result.delivery_id == expected
            except sqlite3.OperationalError as exc:
                if str(exc) != 'interrupted': raise
                status = 'audit_10s_guard'
            elapsed = time.perf_counter() - start
            conn.set_trace_callback(None)
            conn.set_progress_handler(None, 0)
            record = dict(superseded_rows=n, statements=count[0], seconds=elapsed, status=status)
            results.append(record)
            print(json.dumps(record), flush=True)
(out / 'results.json').write_text(json.dumps(results, indent=2) + '\n', encoding='utf-8')
