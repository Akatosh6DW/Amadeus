"""Private helper dispatch before the desktop imports any window/server state."""
import sys


def dispatch(argv=None) -> bool:
    argv = sys.argv if argv is None else argv
    if len(argv) < 3 or argv[1] != '--qq-helper':
        return False
    kind = argv[2]
    if kind not in {'gateway', 'worker', 'qzone'}:
        raise SystemExit('Unsupported QQ helper')
    sys.argv = [argv[0], *argv[3:]]
    if kind == 'gateway':
        from .gateway import main
    elif kind == 'qzone':
        from .qzone_worker import main
    else:
        from .amadeus_worker import main
    main()
    return True
