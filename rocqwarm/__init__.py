"""rocq-warm -- a warm-session wrapper around `rocq`.

Holds a live `rocq repl` parked inside a .v file so that editing a proof
re-executes only from the edit onwards, instead of recompiling the whole file.
See README.md.
"""
