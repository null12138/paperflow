"""支持 ``python -m paperflow`` 启动命令行。"""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
