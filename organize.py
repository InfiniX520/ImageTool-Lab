"""Independent account organizer entry; importing it has no side effects."""
from organizer.cli import main

if __name__=='__main__':
    raise SystemExit(main())
