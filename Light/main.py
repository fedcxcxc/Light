import sys

from backend.runtime import main


# 程序顶层入口：调用后端运行时主函数，并把退出码回传给系统。
if __name__ == "__main__":
    sys.exit(main())
