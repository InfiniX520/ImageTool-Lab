"""Library preflight and backup entry; importing it has no side effects."""
from core.library_maintenance import main

if __name__ == '__main__':
    raise SystemExit(main())
