"""Allow running as: python -m nvcurve"""

import sys

if len(sys.argv) > 1 and sys.argv[1] == "daemon":
    from .daemon import run
    run()
else:
    from .cli import main
    main()
