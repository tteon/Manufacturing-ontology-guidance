# Usage: strace -f -e trace=fsync,fdatasync,write -o out.txt .venv/bin/python src/fsync_probe.py per|flush
# Counts sync syscalls between START/END markers for 100 per-commit SPARQL updates (per) or one flush().
import sys, tempfile, pyoxigraph as ox
mode = sys.argv[1]; p = tempfile.mkdtemp(prefix="oxfsync_")
s = ox.Store(path=p)
s.update('INSERT DATA { GRAPH <http://e/g> { <http://e/o> <http://e/v> 1.0 } }')
print("READY", flush=True)
import os; os.write(2, b"===START===\n")
if mode == "per":
    for i in range(100):
        s.update(f'DELETE {{ GRAPH <http://e/g> {{ <http://e/o> <http://e/v> ?x }} }} INSERT {{ GRAPH <http://e/g> {{ <http://e/o> <http://e/v> {i}.5 }} }} WHERE {{ OPTIONAL {{ GRAPH <http://e/g> {{ <http://e/o> <http://e/v> ?x }} }} }}')
elif mode == "flush":
    s.flush()
os.write(2, b"===END===\n")
